import torch, torch.nn as nn, torch.nn.functional as F
from speachy.lm.tools.train import add_eos, token_lens_to_mask, mark_padding
import numpy as np
from einops import rearrange, repeat
from torch import einsum
from torch.utils.checkpoint import checkpoint # # gradient/activation checkpointing
from functools import partial
import string
from math import ceil
from vector_quantize_pytorch import RandomProjectionQuantizer, VectorQuantize
from typing import Optional, Tuple, List, Dict, Union, Callable

def exists(val):
    return val is not None

# token shifting
# lucidrains implementation: https://github.com/lucidrains/x-transformers/blob/main/x_transformers/x_transformers.py
# BlinkDL idea from RWKV-LM https://github.com/BlinkDL/RWKV-LM
def shift(t, amount, mask = None):
    if amount == 0:
        return t
    else:
        amount = min(amount, t.shape[1])

    if exists(mask):
        t = t.masked_fill(~mask[..., None], 0.)

    return F.pad(t, (0, 0, amount, -amount), value = 0.)

class ShiftTokens(nn.Module):
    '''from Phil Wang's x-transformers library'''
    def __init__(self, shifts, fn):
        super().__init__()
        self.fn = fn
        self.shifts = tuple(shifts)

    def forward(self, x, **kwargs):
        mask = kwargs.get('mask', None)
        shifts = self.shifts
        segments = len(shifts)
        feats_per_shift = x.shape[-1] // segments
        splitted = x.split(feats_per_shift, dim = -1)
        segments_to_shift, rest = splitted[:segments], splitted[segments:]
        segments_to_shift = list(map(lambda args: shift(*args, mask = mask), zip(segments_to_shift, shifts)))
        x = torch.cat((*segments_to_shift, *rest), dim = -1)
        return self.fn(x, **kwargs)


class DynamicPositionBias(nn.Module):
    '''Adapted from Phil Wang's x-transformers library'''
    def __init__(self, dim, *, heads, depth, log_distance = False, norm = False, activation=nn.ReLU):
        super().__init__()
        assert depth >= 1, 'depth for dynamic position bias MLP must be greater or equal to 1'
        self.log_distance = log_distance

        self.mlp = nn.ModuleList([])

        self.mlp.append(nn.Sequential(
            nn.Linear(1, dim),
            nn.LayerNorm(dim) if norm else nn.Identity(),
            activation()
        ))

        for _ in range(depth - 1):
            self.mlp.append(nn.Sequential(
                nn.Linear(dim, dim),
                nn.LayerNorm(dim) if norm else nn.Identity(),
                activation()
            ))

        self.mlp.append(nn.Linear(dim, heads))


    def forward(self, pos, indices, device, dtype):
        pos = pos.to(device=device, dtype=dtype)
        
        if self.log_distance:
            pos = torch.sign(pos) * torch.log(pos.abs() + 1)  # log of distance is sign(rel_pos) * log(abs(rel_pos) + 1)

        for layer in self.mlp:
            pos = layer(pos) 
      
        bias = pos[indices]
        #print(bias.shape)
        bias = rearrange(bias, 'b i j h -> b h i j')
        return bias

class ScaledSinuEmbedding(nn.Module):
    '''taken From Phil Wang's x-transformers library'''
    def __init__(self, dim):
        super().__init__()
        self.scale = nn.Parameter(torch.ones(1,))
        inv_freq = 1. / (10000 ** (torch.arange(0, dim, 2).float() / dim))
        self.register_buffer('inv_freq', inv_freq)

    def forward(self, x):
        n, device = x.shape[1], x.device
        t = torch.arange(n, device = device).type_as(self.inv_freq)
        sinu = einsum('i , j -> i j', t, self.inv_freq)
        emb = torch.cat((sinu.sin(), sinu.cos()), dim = -1)
        return emb * self.scale

class ReLUSquared(nn.Module):
    def forward(self, x):
        return torch.pow(F.relu(x), 2)

def l2norm(t, groups = 1, dim = -1):
    if groups == 1:
        return F.normalize(t, p = 2, dim = dim)
    t = rearrange(t, '... (g d) -> ... g d', g = groups)
    t = F.normalize(t, p = 2, dim = dim)
    return rearrange(t, '... g d -> ... (g d)')



class Attention(nn.Module):
    def __init__(
        self,
        n_feats,
        head_dim,
        n_heads,
        dropout=0.1,
        bias=False,
        return_attention=False,
        causal=False,
        activation='softmax',
        **kwargs
    ):
        super().__init__()
        assert activation in ['relusq', 'softmax']
        self.shared_kv = kwargs.get('shared_kv', False)
        
        self.talking_heads = kwargs.get('talking_heads', 'none') # 'none', 'pre', 'both', 'post' 

        self.n_feats, self.head_dim, self.n_heads = n_feats, head_dim, n_heads
        self.dropout = nn.Dropout(dropout)
        self.bias = bias
        self.return_attention = return_attention
        self.causal = causal

        if self.talking_heads == 'pre' or self.talking_heads == 'both':
            self._head_proj = nn.Conv2d(n_heads, n_heads, (1, 1))
        if self.talking_heads == 'post' or self.talking_heads == 'both':
            self._head_proj_post = nn.Conv2d(n_heads, n_heads, (1, 1))
            

        self.activation = ReLUSquared() if activation == 'relusq' else nn.Softmax(dim=-1)

        if not self.shared_kv:
            self.qkv_proj = nn.Linear(n_feats, 3 * n_heads * head_dim, bias=bias)
            self.qkv = lambda x: rearrange(self.qkv_proj(x), "b n (h d qkv) -> qkv b h n d", qkv=3, h=n_heads, d=head_dim)
        else:
            self.q_proj, self.kv_proj = [nn.Linear(n_feats, el, bias=bias) for el in [n_heads * head_dim, 2 * head_dim]]
            map_q, map_kv = lambda q: rearrange(q, 'b n (h d) -> b h n d', h=n_heads), lambda kv: rearrange(kv, 'b n (kv d) -> kv b () n d', kv=2, d=head_dim)
            self.qkv = lambda x: (map_q(self.q_proj(x)), *map_kv(self.kv_proj(x)))

        self.out_proj = nn.Linear(n_heads * head_dim, n_feats, bias=bias)
    
    def head_proj(self, dots, mode='pre'):
        if mode == 'pre' and (self.talking_heads == 'pre' or self.talking_heads == 'both'):
            dots = self._head_proj(dots)
        if mode == 'post' and (self.talking_heads == 'post' or self.talking_heads == 'both'):
            dots = self._head_proj_post(dots)
        return dots      
  

    def attend(self, query, key, value, attn_mask, pos_bias):        
        dots = einsum('bhid,bhjd->bhij', query, key) * self.head_dim ** -0.5
        dots = self.head_proj(dots, mode='pre')

        dots += pos_bias

        dots.masked_fill_(attn_mask, -torch.finfo(dots.dtype).max)

        attn = self.activation(dots)
        attn = self.head_proj(attn, mode='post')
     
        attn = self.dropout(attn)
        return einsum("bhij,bhjd->bhid", attn, value)

    @staticmethod
    def attach_cache(kv, cache, cache_indices):
        kv = torch.stack(kv, dim=0)
        if cache is None:
            return kv
        zero_vector = torch.zeros_like(kv[:, :, :, :1, :])
        kv_w_cache = torch.cat([cache, kv, zero_vector], dim=-2)
        kv_w_cache = torch.gather(kv_w_cache, dim=-2, index=cache_indices) # we do this to remove unnecessary padding
        return kv_w_cache

    def forward(self, x, pos_bias, mask, cache=None, cache_indices=None):
        B, N, C, H, D = *x.shape, self.n_heads, self.head_dim
    
        q, k, v  = self.qkv(x)
        kv = self.attach_cache([k, v], cache, cache_indices)
        k, v = kv

        out = self.attend(q, k, v, mask, pos_bias)

        out = rearrange(out, "b h n d -> b n (h d)")
        out = self.out_proj(out)
        return out, kv

class PreNorm(nn.Module):
    def __init__(self, dim, fn, elementwise_affine=True):
        super().__init__()
        self.norm = nn.LayerNorm(dim, elementwise_affine=elementwise_affine)
        self.fn = fn

    def forward(self, x, *args, **kwargs):
        return self.fn(self.norm(x), *args, **kwargs)


class GLU(nn.Module):
    def __init__(self, dim_in, dim_out, activation):
        super().__init__()
        self.act = activation
        self.proj = nn.Linear(dim_in, dim_out * 2)

    def forward(self, x):
        x, gate = self.proj(x).chunk(2, dim = -1)
        return x * self.act(gate)

def orthogonal_loss_fn(t):
    # eq (2) from https://arxiv.org/abs/2112.00384
    # fn: https://github.com/lucidrains/vector-quantize-pytorch/blob/master/vector_quantize_pytorch/vector_quantize_pytorch.py
    h, n = t.shape[:2]
    normed_codes = l2norm(t)
    cosine_sim = einsum('h i d, h j d -> h i j', normed_codes, normed_codes)
    return (cosine_sim ** 2).sum() / (h * n ** 2) - (1 / n)

class orthoginal_loss(nn.Module): # same as above but as a module
    def __init__(self, weight=5.0):
        super().__init__()
        self.loss_fn = orthogonal_loss_fn
        self.weight = weight

    def forward(self, t):
        return self.loss_fn(t) * self.weight

class Halfer(nn.Module): # uses conv instead of avg_pool1d
    def __init__(self, dim, exp_f=2):
        super().__init__()
        self.conv = nn.Conv1d(dim, dim*exp_f, kernel_size=2, stride=2, padding=0, bias=True)
        self.act = nn.SiLU()
        self.empty_vec = nn.Parameter(torch.zeros(1,1,dim))
        self.ff = nn.Linear(dim*exp_f, dim)

    def forward(self, x, length):
        if x.shape[1] % 2 == 1:
            x = torch.cat([x, self.empty_vec.expand(x.shape[0],1,-1)], dim=1)
            length += 1
        x = self.conv(x.transpose(1,2)).transpose(1,2)
        x = self.ff(self.act(x))
        length = (length + 1).div(2).floor().long()
        return x, length

class InverseHalfer(nn.Module): # opposite of Halfer
    def __init__(self, dim, exp_f=2):
        super().__init__()
        self.conv = nn.ConvTranspose1d(dim, dim*exp_f, kernel_size=2, stride=2, padding=0, bias=True)
        self.act = nn.SiLU()
        self.ff = nn.Linear(dim*exp_f, dim)

    def forward(self, x, length):
        x = self.conv(x.transpose(1,2)).transpose(1,2)
        x = self.ff(self.act(x))
        length = length.mul(2)
        return x, length

class HalferBlock(nn.Module):
    def __init__(self, dim, exp_f=2):
        super().__init__()
        self.halfer = PreNorm(dim=dim, fn=Halfer(dim, exp_f=exp_f))
        self.inverse_halfer = PreNorm(dim=dim, fn=InverseHalfer(dim, exp_f=exp_f))
        self.loss = nn.MSELoss(reduction='none')

    def forward(self, x, length, mask=None):
        halved_x, halved_length = self.halfer(x, length)
        def recon_loss_fn(quantized_x):
            restored_x, _ = self.inverse_halfer(quantized_x, halved_length)
            restored_x = restored_x[:, :x.shape[1], :] # trim to original length
            loss = torch.tensor([0.], device=x.device)
            if self.training:
                loss = self.loss(restored_x, x)
                if mask is not None: # mask out padding
                    loss.masked_fill_(mask[..., None], 0.)
                loss = loss.mean()
            return loss, restored_x
        return halved_x, halved_length, recon_loss_fn



class PredictionLayer(nn.Module):
    def __init__(self, dim, n_classes):
        super().__init__()
        self.proj = PreNorm(dim, nn.Linear(dim, n_classes))
    def forward(self, x):
        return self.proj(x)

class NextTokenPredictor(nn.Module):
    def __init__(self, n_classes):
        super().__init__()
        self.gamma = nn.Parameter(torch.randn(1,n_classes))
        self.beta = nn.Parameter(torch.randn(1,n_classes))
        nn.init.uniform_(self.gamma, 0.5, 1.5)
        nn.init.uniform_(self.beta, -0.1, 0.1)

    def forward(self, x, length):
        # get the last token in x
        last_logits = x[torch.arange(x.shape[0]), length-1] 
        last_logits = last_logits * self.gamma + self.beta
        return last_logits[:,None,:]



class transformer(nn.Module):
    def __init__(
            self, 
            dim, 
            depth, 
            heads, 
            dim_head, 
            causal=True,
            base_vocab_size=29,
            dropout = 0.1,
            **kwargs
        ):
        super().__init__()
    

        ff_mult = kwargs.get('ff_mult', 4)
        self.checkpoint_every_n = kwargs.get('checkpoint_every_n', 0)
        self.base_vocab = base_vocab_size
        self.max_vocab = kwargs.get('max_vocab', 10000)
        commitment_weight = kwargs.get('commitment_weight', 1.0)

        self.embedding = nn.Embedding(base_vocab_size, dim)

        self.causal = causal

        self.depth = depth
        self.positional_bias = DynamicPositionBias(
            dim = dim // 4,
            heads = heads,
            depth = 2,
            log_distance = False,
            norm = False
        )
        self.halfer = HalferBlock(dim, exp_f=4)
        self.orthogonal_loss = orthoginal_loss(weight=5.0)

        self.vocab_fn = lambda l: min(self.max_vocab, ceil(((self.base_vocab * l)**1.5))) + self.base_vocab
        self.layers = nn.ModuleList([])
        for lth in range(depth):
            print(f'v: {self.vocab_fn(lth)}')
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(
                    dim, 
                    n_heads=heads, 
                    head_dim=dim_head, 
                    causal=causal,
                    dropout=dropout,
                    **kwargs
                )),
                PreNorm(dim, Attention(
                    dim, 
                    n_heads=heads, 
                    head_dim=dim_head, 
                    causal=causal,
                    dropout=dropout,
                    **kwargs
                )),
                PreNorm(dim, self.ff(dim, mult=ff_mult)),
                PreNorm(dim, self.ff(dim, mult=ff_mult)),
                nn.Linear(dim, dim) if lth != 0 else None,
                PredictionLayer(dim, self.vocab_fn(0)) if lth==0 else PredictionLayer(dim, dim),
                NextTokenPredictor(n_classes=self.vocab_fn(lth)),
                PreNorm(dim, VectorQuantize(
                    dim = dim,
                    codebook_dim = 64,
                    codebook_size = self.vocab_fn(lth + 1),
                    commitment_weight=commitment_weight,
                    kmeans_init = True,
                    use_cosine_sim = True,
                    decay=0.9,
                    threshold_ema_dead_code=0.05,
                )) if lth < depth - 1 else None # no vector quantization in the last layer
            ]))

    @staticmethod
    def ff(dim, mult=4, dropout=0.1):
        return nn.Sequential(
            GLU(dim, dim * mult, nn.SiLU()),
            nn.Dropout(dropout),
            nn.Linear(dim * mult, dim)
        )

    @staticmethod
    def create_custom_forward(module):
        def custom_forward(*args, **kwargs):
            return module(*args, **kwargs)
        return custom_forward

    def checkpoint(self, layer, module, *args, **kwargs):
        condition = self.training and self.checkpoint_every_n != 0 and layer < self.depth - 1 and layer % self.checkpoint_every_n == 0
        return checkpoint(self.create_custom_forward(module), *args, **kwargs) if condition else module(*args, **kwargs)

    @staticmethod
    def get_cache(cache):
        if cache is None:
            return None
        return cache['cache'][0]

    @staticmethod
    def get_cache_indices(x_lens, cache_lens, cache_kv, x):  
        # used later w/ gather to remove padding when cache is concatenated with current input to remove padding
        max_new_len = (x_lens + cache_lens).max()
        # cache kv =  LAYERS, KEYS+VALUES (2), BATCH, HEADS, N, DIM
        B, H, N, D = x.shape[0], cache_kv.shape[-3], (x.shape[1] + cache_kv.shape[-2]), cache_kv.shape[-1]
        indices = []
        for i in range(B): # stinky for loop to sort out indices for gather 
            cache_indices = torch.arange(cache_lens[i], device='cpu')
            total_length = cache_lens[i] + x_lens[i] 
            diff_from_max_len = max_new_len - total_length
            x_indices = torch.arange(x_lens[i]+diff_from_max_len, device='cpu') + cache_kv.shape[-2]
            if diff_from_max_len > 0:
                x_indices[-diff_from_max_len:] = N  # last index will be used for padding
            new_indices = torch.cat([cache_indices, x_indices])
            indices.append(new_indices)

        indices = torch.stack(indices, dim=0)
        
        indices = rearrange(indices, 'b n -> () b () n ()').expand(2, B, H,-1, D) # 2 for key and value
        return indices.to(x.device)

    def create_masks_and_positions(self, x, length, cache): 
        x_len = length if length is not None else torch.tensor(x.shape[-2]).expand(x.shape[0])
        cache_len = cache['cache_lengths'] if exists(cache) else 0

        total_len = x_len + cache_len
        kv_mask = torch.arange(total_len.max(), device=x.device).expand(len(total_len), -1) >= total_len.unsqueeze(-1)
        q_mask = torch.arange(x_len.max(), device=x.device).expand(len(x_len), -1) >= x_len.unsqueeze(-1)
        attn_mask = ~(rearrange(~q_mask, "b n -> b () n ()") * rearrange(~kv_mask, "b n -> b () () n"))
        ##
        ##
        causal_mask = repeat(torch.arange(total_len.max(), device=x.device), 'i -> b r i', b=len(total_len), r=x_len.max())
        cache_offset = cache_len[:,None,None] if exists(cache) else cache_len
        diagonal_offset = torch.arange(x_len.max(), device=x.device)[None,:,None]
        ##
        ## positional stuff ##
        positional_grid = (causal_mask - cache_offset - diagonal_offset) * -1
        pos = torch.arange(positional_grid.min(), positional_grid.max()+1, device=x.device, dtype=x.dtype)[:,None]
        min_cache_len = 0 if cache_len.__class__ == int else cache_len.min()
        positional_indices = ((positional_grid) + (total_len.max() - min_cache_len - 1)) # shift so zero is the smallest number
        pos_bias = self.positional_bias(pos=pos, indices=positional_indices, dtype=x.dtype, device=x.device)
        ## positional stuff ##
        ##
        if self.causal:
            causal_mask = causal_mask >= (cache_offset + diagonal_offset + 1)
            attn_mask = torch.logical_or(attn_mask, causal_mask[:,None])
        ##
        return q_mask, attn_mask, total_len, x_len, cache_len, pos_bias


    def forward(self, x, length, cache=None, **kwargs):
        
        intermediate_logits = []
        layer_below_predictions = []
        next_token_preds = []
        
        intermediate_targets = [None] # for the first layer we use ground truth tokens as targets
        commitment_loss = []

        cache_lengths = []
        lengths = [length.clone()]
        cached_kvs = []

        curcache = cache[0] if exists(cache) else None
        mask, attn_mask, total_lens, x_len, cache_len, pos_bias = self.create_masks_and_positions(x, length, curcache)
        cache_indices = self.get_cache_indices(x_len, cache_len, curcache['cache'], x) if exists(curcache) else None
    
        for i, (attn1, attn2, ff1, ff2, blpred, predl, ntpred, vq) in enumerate(self.layers):
            
            ## attention ff blocks ##
            a_out, kv = self.checkpoint(i, attn1, x, pos_bias, attn_mask, self.get_cache(curcache), cache_indices)
            x = a_out + x
            cached_kvs.append(kv[None])
            cache_lengths.append(total_lens)
            x = self.checkpoint(i, ff1, x) + x 
            a_out, kv = self.checkpoint(i, attn2, x, pos_bias, attn_mask, self.get_cache(curcache), cache_indices)
            x = a_out + x
            cached_kvs.append(kv[None])
            cache_lengths.append(total_lens)
            x = self.checkpoint(i, ff2, x) + x
            ## attention ff blocks ##

            if i == 0:
                pred = self.checkpoint(i, predl, x)
                intermediate_logits.append(pred)
                ntp = ntpred(pred, length)
                next_token_preds.append(ntp)
            else:
                pred_emb_proj = self.checkpoint(i, predl, x)
                belowvq = self.layers[i-1][-1]
                pred_emb = belowvq.fn.project_in(belowvq.norm(pred_emb_proj))
                pred_sim = einsum('bnd, vd -> bnv', pred_emb, belowvq.fn.codebook.detach())
                intermediate_logits.append(pred_sim)
                ntp = ntpred(pred_sim, length)
                next_token_preds.append(ntp)
        
            if i > 0: # decomposed into layer belows sequence
                inverse_halfer = self.halfer.inverse_halfer
                lb_x, _, lb_lengths = *inverse_halfer(pred_emb_proj, length), lengths[-2]
                lb_x_d = self.checkpoint(i, blpred, lb_x)
                lb_x_trim = lb_x_d[:, :lb_lengths.max()]
                if i-1!=0:
                    belowvq = self.layers[i-2][-1]
                    lb_x_cb = belowvq.fn.project_in(belowvq.norm(lb_x_trim))
                    lb_cb_pred = einsum('bnd, vd -> bnv', lb_x_cb, belowvq.fn.codebook.detach())
                    layer_below_predictions.append(lb_cb_pred)
                else:
                    lbp = einsum('bnd, vd -> bnv', lb_x_trim, self.embedding.weight.detach())
                    layer_below_predictions.append(lbp)
              
            if exists(vq):
                x, length, recon_loss_fn = self.halfer(x=x, length=length, mask=mask)

                lengths.append(length.clone())
                curcache = cache[i+1] if exists(cache) else None
                mask, attn_mask, total_lens, x_len, cache_len, pos_bias = self.create_masks_and_positions(x, length, curcache)
            
                cache_indices = self.get_cache_indices(x_len, cache_len, curcache['cache'], x) if exists(curcache) else None

                _, indices, commit_loss = vq(x)
                recon_loss, _ = recon_loss_fn(x) # loss for reconstruction of x components compared to fused and quantized x
                #commit_loss = torch.tensor([0.], device=x.device)
                orth_loss = self.checkpoint(i, self.orthogonal_loss, x) # prevent mode collapse
                commitment_loss.append(recon_loss.sum() + commit_loss.sum() + orth_loss.sum())
                intermediate_targets.append(indices)
                

        #print(len(cached_kvs), len(layer_below_next_token_preds), len(cache_lengths), len(next_token_preds))
        assert len(cached_kvs) == len(cache_lengths), 'something went wrong'
  
        cached_kvs = [{'cache': curcache, 'cache_lengths': curlen} for curcache, curlen in zip(cached_kvs, cache_lengths)]
        cached_kvs = {'layers': cached_kvs, 'next_sentence_pred': next_token_preds}

        return {
            'logits': intermediate_logits,
            'layer_below_predictions': [*layer_below_predictions, None],
            'targets': intermediate_targets,
            'cache': cached_kvs,
            'commitment_loss': torch.stack([*commitment_loss,torch.tensor(0.,device=commitment_loss[0].device)]),
            'lengths': torch.stack(lengths)
        }


class transformer_lm(nn.Module):
    def __init__(
        self,
        dim,
        vocab_size,
        depth,
        heads,
        dim_head,
        causal=True,
        dropout=0.,
        use_abs_pos=False,
        **kwargs
    ):
        super().__init__()
    
        self.use_abs_pos = use_abs_pos
        if self.use_abs_pos:
            self.abs_pos_fn = ScaledSinuEmbedding(dim=dim)
        self.abs_pos = lambda x: x + self.abs_pos_fn(x) if self.use_abs_pos else x


        self.layers = transformer(
            dim = dim, 
            depth = depth, 
            heads = heads, 
            dim_head = dim_head, 
            causal = causal, 
            dropout = dropout,
            base_vocab_size = vocab_size,
            **kwargs
        )

        

    def count_parameters(self):
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

   
    def calc_all_losses(self, tlm_out, prev_cache):
        eos_id = -100
        loss_fn = lambda l, t: F.cross_entropy(rearrange(l, 'b n c -> b c n'), t, ignore_index=-100, reduction='mean')
        losses = []
        lbelow_losses = []

        def calc_token_loss(logits, targets, first_token_pred, length):
            if length.max() == 1 and not exists(first_token_pred):
                return None # no loss for single token sequences if no previous prediction is available
            if exists(first_token_pred): # concat zero vect to end of targets
                targets = torch.cat([targets, torch.zeros(targets.size(0), 1, dtype=targets.dtype, device=targets.device)], dim=1)
                logits = torch.cat([first_token_pred, logits], dim=1)
                length += 1
            else:
                targets[:,:-1] = targets.clone()[:,1:]
            targets = add_eos(targets, eos_id=eos_id, token_lens=length)
            targets = mark_padding(targets=targets, mask=token_lens_to_mask(token_lens=length), pad_id=eos_id)
            loss = loss_fn(logits, targets)
            
            return loss

        def calc_layer_below_loss(logits, targets, length):
            if length.max() <= 2:
                return torch.tensor(0., device=logits.device)
            targets[:,:-2] = targets.clone()[:,2:] # shift targets by two
            targets = add_eos(targets, eos_id=eos_id, token_lens=length)
            targets = mark_padding(targets=targets, mask=token_lens_to_mask(token_lens=length-2, max_len=length.max()), pad_id=eos_id)
            loss = loss_fn(logits, targets)
            if loss.isnan():
                loss = torch.tensor(0., device=logits.device)
            return loss

        if exists(prev_cache):
            assert len(tlm_out['logits']) == len(prev_cache['next_sentence_pred']), 'something went wrong'
        for lth in range(len(tlm_out['logits'])):
            logits = tlm_out['logits'][lth]
            lbelow_logits = tlm_out['layer_below_predictions'][lth]
            targets = tlm_out['targets'][lth]
            
            #print(targets.reshape(-1).unique().shape)
            first_token_pred = prev_cache['next_sentence_pred'][lth] if exists(prev_cache) else None
            
            lengths = tlm_out['lengths'][lth]
            loss = calc_token_loss(logits, targets.clone(), first_token_pred, lengths.clone())
            lbelow_loss = calc_layer_below_loss(lbelow_logits, targets.clone(), lengths.clone()) if exists(lbelow_logits) else torch.tensor(0., device=logits.device)
            if exists(loss): # incase of single token sequences
                losses.append(loss)
                lbelow_losses.append(lbelow_loss)
                
        tlm_out['token_losses'] = torch.stack(losses) + torch.stack(lbelow_losses)

        return tlm_out


    def forward(self, labels, length, cache:Dict=None, calc_loss=False, **kwargs):
        '''
        x: [B, N] (embedding indices)
        length: [B] (length of each sequence)
        cache: {cache_lengths: [B, N], cache: [L, KV, B, H, N, D]} KV: key and value (2)
        '''
        assert labels.shape[1] == length.max(), 'sequence length should be equal to the length of the longest sequence!'
        x = self.layers.embedding(labels)
        x = self.abs_pos(x) 
        
        outputs = self.layers(x, length, cache=cache['layers'] if exists(cache) else None, **kwargs)
        outputs['targets'][0] = labels.clone() # for the first layer we use ground truth tokens as targets
        if calc_loss:
            outputs = self.calc_all_losses(tlm_out=outputs, prev_cache=cache)
   
        return outputs

class CharacterTokenizer(): # only for testing!
    def __init__(self):
        self.vocab = ['#', '/'] + list(string.ascii_lowercase) + [' '] # bos/eos -> /, pad -> #
        self.vocab_size = len(self.vocab)
        self.token_to_id = {token: i for i, token in enumerate(self.vocab)}
        self.id_to_token = {i: token for i, token in enumerate(self.vocab)}
    
    def __call__(self, text):
        return self.tokenize(text)

    def tokenize(self, text):
        return [self.token_to_id[token] for token in text]

def collate_fn(tensors:List[torch.Tensor], pad_token:int): # only for testing!
    max_len = max([t.shape[0] for t in tensors])
    lengths = torch.tensor([t.shape[0] for t in tensors])
    padded_tensors = [torch.cat([t, torch.full((max_len - t.shape[0],), pad_token, dtype=t.dtype)], dim=0) for t in tensors]
    return torch.stack(padded_tensors, dim=0), lengths


@torch.no_grad()
def caching_test():
    tokenizer = CharacterTokenizer()
    model = transformer_lm(
        dim = 256,
        vocab_size = tokenizer.vocab_size,
        depth = 10,
        heads = 1,
        dim_head = 32,
        dropout=0.0,
        causal = True,
        shared_kv = True,
    )
    model.eval()
    # test batches to test caching
    s1_b1, s2_b1, s3_b1 = torch.tensor(tokenizer('/hi')), torch.tensor(tokenizer('/buenos')), torch.tensor(tokenizer('/whats'))
    s1_b2, s2_b2, s3_b2 = torch.tensor(tokenizer(' there')), torch.tensor(tokenizer(' dias')), torch.tensor(tokenizer(' up'))
    s1_b3, s2_b3, s3_b3 = torch.tensor(tokenizer(' how')), torch.tensor(tokenizer(' captain')), torch.tensor(tokenizer(' donkey'))
    s1_b4, s2_b4, s3_b4 = torch.tensor(tokenizer(' u/')), torch.tensor(tokenizer(' hook/')), torch.tensor(tokenizer(' man/'))
    b1, b1_lengths = collate_fn([s1_b1, s2_b1, s3_b1], pad_token=tokenizer.token_to_id['#'])
    b2, b2_lengths = collate_fn([s1_b2, s2_b2, s3_b2], pad_token=tokenizer.token_to_id['#'])
    b3, b3_lengths = collate_fn([s1_b3, s2_b3, s3_b3], pad_token=tokenizer.token_to_id['#'])
    b4, b4_lengths = collate_fn([s1_b4, s2_b4, s3_b4], pad_token=tokenizer.token_to_id['#'])
    # comparsion set final states of above should be the same as these
    f_1, f_2, f_3 = torch.tensor(tokenizer('/hi there how u/')), torch.tensor(tokenizer('/buenos dias captain hook/')), torch.tensor(tokenizer('/whats up donkey man/'))
    fb, fb_lengths = collate_fn([f_1, f_2, f_3], pad_token=tokenizer.token_to_id['#'])

    logits_s1, interim_logits, cached_kvs = model(b1, length=b1_lengths)
    logits_s2, interim_logits, cached_kvs_s2 = model(b2, length=b2_lengths, cache=cached_kvs)
    logits_s3, interim_logits, cached_kvs_s3 = model(b3, length=b3_lengths, cache=cached_kvs_s2)
    logits_s4, interim_logits, cached_kvs_s4 = model(b4, length=b4_lengths, cache=cached_kvs_s3)
    logits_fs, interim_logits, cached_kvs_fs = model(fb, length=fb_lengths)

    print('shapes: ', cached_kvs_fs['cache'].shape, cached_kvs_s4['cache'].shape)
    c_lens = cached_kvs_fs['cache_lengths']
    mask = torch.arange(c_lens.max())[:,None] < c_lens[None,:]
    mask = ~mask.T
    mask = rearrange(mask, 'b i -> () () b () i ()')
    fs_cache =  cached_kvs_fs['cache'].masked_fill(mask, 0)

    assert torch.allclose(fs_cache, cached_kvs_s4['cache'], atol=0.001), 'failed check ): ): ):'
    print('things are looking up !')