import argparse
import torch
from nemo.collections.asr.models.ctc_bpe_models import EncDecCTCModelBPE
from nemo.collections.asr.models.rnnt_bpe_models import EncDecRNNTBPEModel
import tools
import os
from tqdm import tqdm
import numpy as np
from typing import Dict, List, Optional, Tuple, Union

import wandb
import kenlm

import multiprocessing
import nemo

from nemo.collections.asr.metrics.wer import word_error_rate
from omegaconf.omegaconf import OmegaConf
from model_utils import load_checkpoint, load_nemo_checkpoint, load_model, load_sc_model, write_to_log

#import non_iid_dataloader
from speachy.asr.dataloading import non_iid_dataloader
import nemo.collections.asr as nemo_asr

import pickle as pkl

from tools import isfalse, istrue, exists, save_json
from nemo.collections.asr.metrics.wer import word_error_rate

import speachy
from speachy.ctc_beam_search import BeamSearch, LanguageModel

from functools import partial
import ray
import random


@torch.no_grad()
def get_logits(args, model, corpus):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model.to(device)
    model.eval()
    hyp_data = []

    dataloader = non_iid_dataloader.get_eval_dataloader(
        corpus, 
        max_duration=args.max_duration, 
        return_speaker=True, 
        batch_size=1, 
        concat_samples=args.concat_samples,
        split_speakers=args.split_speakers,
        gap=args.gap,
        speaker_gap=args.speaker_gap,
        single_speaker_with_gaps=args.single_speaker_with_gaps,
        return_meta_data=True,
        max_allowed_utterance_gap=args.max_allowed_utterance_gap,
    )

    utt_num = 0
    pbar = tqdm(dataloader, total=len(dataloader))
    for batch_num, batch in enumerate(pbar):
        audios = batch['audio'].reshape(1, -1).to(device)
        audio_lengths = batch['audio_lens'].reshape(1).to(device)
        speaker_ids = ["_".join(el[0]) for el in batch['speakers']]
        targets = [el[0] for el in batch['text']]
        targets = [el.replace(" '", "'") for el in targets] # change this in training so that it's not needed here but i'll keep it for now
        meta_data = batch['metadata'][0][0]
        meta_data['timings'] = {'segment_start':meta_data['timings'][0]['segment_start'], 'segment_end':meta_data['timings'][-1]['segment_end']}
        meta_data['speaker'] = list(set(meta_data['speaker']))
        meta_data['recording_id'] = meta_data['recording_id'][0]
        model_out = model.forward(
            input_signal=audios, 
            input_signal_length=audio_lengths,
            segment_lens=batch['segment_lens'] if isfalse(args.do_not_pass_segment_lens) else None
        )
        log_probs, _, encoded_len = model_out[:3]
        s_lprobs = log_probs.squeeze().cpu()
        outputs = {
            'meta_data': meta_data,
            'speaker_ids': speaker_ids,
            'targets': targets,
            'batch_num': batch_num,
            'probs': s_lprobs,
        }
        hyp_data.append(outputs)

    return hyp_data


def load_pickle(path):
    with open(path, 'rb') as f:
        pkl_data = pkl.load(f)
    return pkl_data['stage'], pkl_data['data']

def save_pickle(path, obj, stage='logits'):
    with open(path, 'wb') as f:
        pkl.dump({'stage':stage, 'data':obj}, f) if stage != 'finished' else pkl.dump(obj, f)

def delete_pickle(path):
    os.remove(path)

class argsclass():
    def __init__(self, args:Dict): self.__dict__.update(args)

@ray.remote(num_gpus=0, num_cpus=1)
def run_search(beam_fn, logps, alpha, beta):
    search = beam_fn(log_probs=logps, alpha=alpha, beta=beta)
    search.run_search(use_tqdm=False)
    return search.return_text(0)

def write_to_log(log_path, text):
    with open(log_path, 'a') as f:
        f.write(text+'\n')

def main(args):
    model = load_model(args) if args.self_conditioned == False else load_sc_model(args)
    if args.checkpoint != '':
        load_checkpoint(args, model)
    print('\nTrainable parameters:'+str(sum(p.numel() for p in model.parameters() if p.requires_grad)))
    print(f'Total parameters: {sum(p.numel() for p in model.parameters())}\n')
    corpus_dict = tools.load_corpus()
  

    config = speachy.utils.general.load_config('../experiment_configs/lm/decoder_pg19_sep_token.yaml')
    tokenizer_path = '.'+os.path.join(config['model']['tokenizer']['dir'], 'tokenizer.model')
    tokenizer = speachy.utils.general.load_tokenizer(tokenizer_path)
    lm_model = speachy.lm.tools.loading.autoload(config=config, tokenizer=tokenizer)
    _,_ = speachy.utils.general.load_checkpoint(
        args = argsclass({'checkpoint':'../checkpoints/open_sub_ft_ami/128subword_ami_opensub_802_78.pt'}),
        model = lm_model,
        force_cpu = True
    )

    temp_name_dev = f'dev_{args.load_tmp}'
    dev_stage, test_stage = None, None # stage corresponds to the last step of the pipeline that was completed
    print(f'Fetching logits for dev set...')
    if os.path.exists(os.path.join(args.tmp_dir, temp_name_dev)):
        dev_stage, dev_hyps = load_pickle(os.path.join(args.tmp_dir, temp_name_dev))

    if dev_stage == None:
        dev_hyps = get_logits(args, model, corpus_dict['test'])
        save_pickle(os.path.join(args.tmp_dir, temp_name_dev), dev_hyps, stage='logits')
        dev_stage = 'logits'
        
    del model
    alpha_range = [0.43259, 0.43261]
    beta_range = [0.5, 0.50000000000000001]

    write_to_log('beam_search_log.txt', f'alpha_range: {alpha_range}, beta_range: {beta_range} Initialising beam search...')
    
    
    ray.init(num_cpus=40, num_gpus=0)
    
    beamsearch_fn = partial(
        BeamSearch, 
        language_model=LanguageModel(model=lm_model, bos_id=0, device='cuda' if torch.cuda.is_available() else 'cpu'),
        tokenizer=tokenizer, 
        beam_width=25,
        blank_id=128,
        blank_penalty=0.0,
        repitition_penalty=0.0,
        top_am_threshold=-5,
        debug=False
    )
    beamsearch_fn = ray.put(beamsearch_fn) # put beamsearch_fn on the ray object store so that it can be accessed by the remote function
    # select random sample of dev hyps (10%)
    # runs same random seed
    random.seed(42)
    #dev_hyps_sample = #random.sample(dev_hyps, int(len(dev_hyps)*0.05))
    dev_hyps_sample = dev_hyps
    while True:
        alpha = np.random.uniform(*alpha_range)
        beta = np.random.uniform(*beta_range)
        write_to_log('beam_search_log.txt', f'alpha: {alpha}, beta: {beta}')
        # split dev_hyps into batches/chunks of 20 
        # then run beam search on each chunk
        chunksize = 250
        batches = [dev_hyps_sample[i:i+chunksize] for i in range(0, len(dev_hyps_sample), chunksize)]

        for batch in tqdm(batches):
            futures = [run_search.remote(beamsearch_fn, hyp['probs'], alpha, beta) for hyp in batch]
            results = ray.get(futures)
            for hyp, result in zip(batch, results):
                hyp['prediction'] = result
                print(hyp['prediction'])
                print(hyp['targets'][0])
                print('')
          

        predictions = [hyp['prediction'] for hyp in dev_hyps_sample]
        targets = [hyp['targets'][0] for hyp in dev_hyps_sample]
        wer = word_error_rate(hypotheses=predictions, references=targets)
        print(f'WER: {wer}')
        write_to_log('beam_search_log.txt', f'WER: {wer} w/ alpha: {alpha}, beta: {beta}')


        
    #save_pickle(os.path.join(args.tmp_dir, args.load_tmp+'_devBEAM.pkl'), dev_hyps, stage='finished')



'''' old
    for hyp in tqdm(dev_hyps):
        lps = hyp['probs']
        beamsearch = beamsearch_fn(log_probs=lps)
        beamsearch.run_search(tqdm=False)
        hyp['prediction'] = beamsearch.return_text(0) # top beam
        print(hyp['prediction'])
        print(hyp['targets'])
'''
    


#Best fp_alpha: 0.9, beta: 0.7, sp_alpha: 0.6, unk_offset: -18, wer: 0.09889438302127895 (beam 1000)




if __name__ == '__main__':
    ''''
    Note I've only written this for a batch size of 1 (lazy)
    '''
    parser = argparse.ArgumentParser() 


    parser.add_argument('-load_tmp', '--load_tmp', default='', type=str, help='base name of logit hyp to load (full name = split+_+name')
    parser.add_argument('-tmp_dir','--tmp_dir', type=str, default='./tmp', help='path to tmp dir')

    parser.add_argument('-log_beta', '--log_beta', action='store_true', help='whether to use log scale for beta length penalty')

    parser.add_argument('--load_pretrained', action='store_true')
    parser.add_argument('--pretrained', type=str, default='stt_en_conformer_ctc_small') # stt_en_conformer_ctc_large stt_en_conformer_transducer_large
    parser.add_argument('--model_config', type=str, default='../model_configs/conformer_sc_ctc_bpe_small.yaml') 

    parser.add_argument('--tokenizer_model', type=str, default='./tokenizers/tokenizer_spe_bpe_v128/tokenizer.model', help='path to tokenizer model')
    parser.add_argument('--max_duration', type=float, default=0, help='max duration of audio in seconds')


    parser.add_argument('--log_file', type=str, default='eval_log.txt')
    parser.add_argument('--checkpoint_dir', type=str, default='./checkpoints')
    parser.add_argument('--checkpoint', type=str, default='checkpoint_68_id_15.pt')
    

    parser.add_argument('--beam_size', type=int, default=300)
    parser.add_argument('--bpe_lm_path', type=str, default='./ngrams/binary_bpe/ami_6grambpe.bin')

    parser.add_argument('-lm', '--language_model', type=str, default='./ngrams/cantab_interp_ami.arpa', help='arpa n-gram model for decoding')#./ngrams/3gram-6mix.arpa
    parser.add_argument('--split', type=str, default='test')
    parser.add_argument('--alpha', type=float, default=0.5)
    parser.add_argument('--beta', type=float, default=0.8)

    parser.add_argument('-token_skip', '--token_min_logp', default=-5, type=float)
    parser.add_argument('-beam_prune', '--beam_prune_logp', default=-10000, type=float)

    parser.add_argument('-nsc','--not_self_conditioned', action='store_true', help='use for non self-conditioned models')

    parser.add_argument('-mgap','--max_allowed_utterance_gap', type=float, default=3.0, help='max allowed gap between utterances in seconds')


    parser.add_argument('-gap','--gap', default=0.1, type=float, help='gap between utterances when concatenating')

    parser.add_argument('--single_speaker_with_gaps', action='store_true', help='if set, utterances will contain 1 speaker and additional gaps of speaker_gap will be added if there is a speaker change between two utternces of the same speaker')
    parser.add_argument('--speaker_gap', type=float, default=1.0, help='for use with single_speaker_with_gaps, will add this many seconds of silence between utterances of the same speaker when there is a speaker change in between them')

    parser.add_argument('--split_speakers', action='store_true', help='if set, wont concat samples from different speakers, (concat_samples must be enabled)')

    parser.add_argument('-psl','--pass_segment_lengths', action='store_true', help='if set, will pass segment lens to the model, used with concat_samples for multi segment models')
    parser.add_argument('-save','--save_outputs', default='', type=str, help='save outputs to file')
    parser.add_argument('-dropout', '--dropout_rate', help='dropout at inference', default=0.0, type=float)

    args = parser.parse_args()

    assert args.language_model != '' and args.bpe_lm_path != '', 'Must provide a language model and a bpe language model'

    args.do_not_pass_segment_lens = not args.pass_segment_lengths
    args.self_conditioned = not args.not_self_conditioned
    args.concat_samples = True
    args.config_from_checkpoint_dir = True
    

    '''if args.save_outputs == '':
        save_outputs = ''
        while save_outputs == '':
            save_outputs = input('Please provide a name for the output file: ').strip()
        args.save_outputs = save_outputs'''

    if os.path.exists(args.tmp_dir) == False:
        os.mkdir(args.tmp_dir)

    if args.checkpoint != '':
        args.checkpoint = os.path.join(args.checkpoint_dir, args.checkpoint)



    if args.config_from_checkpoint_dir == True:
        dir_contents = os.listdir(args.checkpoint_dir)
        config = [el for el in dir_contents if el.endswith('.yaml')]
        assert len(config) == 1, 'Exactly one config file must be in checkpoint dir'
        args.model_config = os.path.join(args.checkpoint_dir, config[0])
        print(f'Loading config from checkpoint dir: {args.model_config}')

    main(args)
