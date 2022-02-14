import torch
import nni.retiarii.nn.pytorch as nn
from nni.retiarii import model_wrapper
import itertools
import math
import warnings

def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    r"""Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the
    normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
    with values outside :math:`[a, b]` redrawn until they are within
    the bounds. The method used for generating the random values works
    best when :math:`a \leq \text{mean} \leq b`.
    Args:
        tensor: an n-dimensional `torch.Tensor`
        mean: the mean of the normal distribution
        std: the standard deviation of the normal distribution
        a: the minimum cutoff value
        b: the maximum cutoff value
    Examples:
        >>> w = torch.empty(3, 5)
        >>> nn.init.trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

def drop_path(x, drop_prob: float = 0., training: bool = False):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).

    This is the same as the DropConnect impl I created for EfficientNet, etc networks, however,
    the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ... I've opted for
    changing the layer and argument names to 'drop path' rather than mix DropConnect as a layer name and use
    'survival rate' as the argument.

    """
    if drop_prob == 0. or not training:
        return x
    keep_prob = 1 - drop_prob
    shape = (x.shape[0],) + (1,) * (x.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    random_tensor.floor_()  # binarize
    output = x.div(keep_prob) * random_tensor
    return output

class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    """
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)

def calc_dropout(dropout, sample_embed_dim, super_embed_dim):
    return dropout * 1.0 * sample_embed_dim / super_embed_dim

def gelu(x: torch.Tensor) -> torch.Tensor:
    if hasattr(torch.nn.functional, 'gelu'):
        return torch.nn.functional.gelu(x.float()).type_as(x)
    else:
        return x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))



class AttentionSuper(nn.Module):
    def __init__(self, embed_dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., normalization = False, relative_position = False,
                 num_patches = None, max_relative_position=14, scale=False, change_qkv = False):
        super().__init__()
        self.num_heads = num_heads
        self.embed_dim = embed_dim

        self.fc_scale = scale
        self.change_qkv = change_qkv
        if change_qkv:
            self.qkv = nn.Linear(embed_dim, num_heads * 64 * 3, bias=qkv_bias)
            self.proj = nn.Linear(num_heads * 64, embed_dim)
            self.sample_scale = 64 ** -0.5
        else:
            self.qkv = nn.Linear(embed_dim, embed_dim * 3, bias=qkv_bias)
            self.proj = nn.Linear(embed_dim, embed_dim)
            self.sample_scale = (embed_dim() // self.num_heads) ** -0.5

        self.relative_position = relative_position
        # if self.relative_position:
        #     self.rel_pos_embed_k = RelativePosition2D_super(super_embed_dim //num_heads, max_relative_position)
        #     self.rel_pos_embed_v = RelativePosition2D_super(super_embed_dim //num_heads, max_relative_position)
        # self.max_relative_position = max_relative_position
        # self.sample_qk_embed_dim = None
        # self.sample_v_embed_dim = None
        # self.sample_num_heads = None
        # self.sample_scale = None
        # self.sample_in_embed_dim = None


        self.attn_drop = nn.Dropout(attn_drop)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.sample_num_heads, -1).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        attn = (q @ k.transpose(-2, -1)) * self.sample_scale
        # if self.relative_position:
        #     r_p_k = self.rel_pos_embed_k(N, N)
        #     attn = attn + (q.permute(2, 0, 1, 3).reshape(N, self.sample_num_heads * B, -1) @ r_p_k.transpose(2, 1)) \
        #         .transpose(1, 0).reshape(B, self.sample_num_heads, N, N) * self.sample_scale

        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1,2).reshape(B, N, -1)
        # if self.relative_position:
        #     r_p_v = self.rel_pos_embed_v(N, N)
        #     attn_1 = attn.permute(2, 0, 1, 3).reshape(N, B * self.sample_num_heads, -1)
        #     # The size of attention is (B, num_heads, N, N), reshape it to (N, B*num_heads, N) and do batch matmul with
        #     # the relative position embedding of V (N, N, head_dim) get shape like (N, B*num_heads, head_dim). We reshape it to the
        #     # same size as x (B, num_heads, N, hidden_dim)
        #     x = x + (attn_1 @ r_p_v).transpose(1, 0).reshape(B, self.sample_num_heads, N, -1).transpose(2,1).reshape(B, N, -1)

        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class TransformerEncoderLayer(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, dropout=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, pre_norm=True, scale=False,
                 relative_position=False, change_qkv=False, max_relative_position=14):
        super().__init__()

        # the configs of super arch of the encoder, three dimension [embed_dim, mlp_ratio, and num_heads]
        self.super_embed_dim = dim
        self.super_mlp_ratio = mlp_ratio
        self.super_ffn_embed_dim_this_layer = int(mlp_ratio * dim)
        self.super_num_heads = num_heads
        self.normalize_before = pre_norm
        self.super_dropout = attn_drop
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.scale = scale
        self.relative_position = relative_position

        # the configs of current sampled arch
        self.sample_embed_dim = None
        self.sample_mlp_ratio = None
        self.sample_ffn_embed_dim_this_layer = None
        self.sample_num_heads_this_layer = None
        self.sample_scale = None
        self.sample_dropout = None
        self.sample_attn_dropout = None

        self.is_identity_layer = None
        self.attn = AttentionSuper(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop,
            proj_drop=dropout, scale=self.scale, relative_position=self.relative_position, change_qkv=change_qkv,
            max_relative_position=max_relative_position
        )

        self.attn_layer_norm = nn.LayerNorm(dim)
        self.ffn_layer_norm = nn.LayerNorm(dim)
        self.activation_fn = gelu

        self.fc1 = nn.Linear(dim, dim * mlp_ratio)
        self.fc2 = nn.Linear(dim * mlp_ratio, dim)


    def set_sample_config(self, is_identity_layer, sample_embed_dim=None, sample_mlp_ratio=None, sample_num_heads=None, sample_dropout=None, sample_attn_dropout=None, sample_out_dim=None):

        if is_identity_layer:
            self.is_identity_layer = True
            return

        self.is_identity_layer = False

        self.sample_embed_dim = sample_embed_dim
        self.sample_out_dim = sample_out_dim
        self.sample_mlp_ratio = sample_mlp_ratio
        self.sample_ffn_embed_dim_this_layer = int(sample_embed_dim*sample_mlp_ratio)
        self.sample_num_heads_this_layer = sample_num_heads

        self.sample_dropout = sample_dropout
        self.sample_attn_dropout = sample_attn_dropout
        self.attn_layer_norm.set_sample_config(sample_embed_dim=self.sample_embed_dim)

        self.attn.set_sample_config(sample_q_embed_dim=self.sample_num_heads_this_layer*64, sample_num_heads=self.sample_num_heads_this_layer, sample_in_embed_dim=self.sample_embed_dim)

        self.fc1.set_sample_config(sample_in_dim=self.sample_embed_dim, sample_out_dim=self.sample_ffn_embed_dim_this_layer)
        self.fc2.set_sample_config(sample_in_dim=self.sample_ffn_embed_dim_this_layer, sample_out_dim=self.sample_out_dim)

        self.ffn_layer_norm.set_sample_config(sample_embed_dim=self.sample_embed_dim)


    def forward(self, x):
        """
        Args:
            x (Tensor): input to the layer of shape `(batch, patch_num , sample_embed_dim)`

        Returns:
            encoded output of shape `(batch, patch_num, sample_embed_dim)`
        """
        if self.is_identity_layer:
            return x

        # compute attn
        # start_time = time.time()

        residual = x
        x = self.maybe_layer_norm(self.attn_layer_norm, x, before=True)
        x = self.attn(x)
        x = F.dropout(x, p=self.sample_attn_dropout, training=self.training)
        x = self.drop_path(x)
        x = residual + x
        x = self.maybe_layer_norm(self.attn_layer_norm, x, after=True)

        residual = x
        x = self.maybe_layer_norm(self.ffn_layer_norm, x, before=True)
        x = self.activation_fn(self.fc1(x))
        x = F.dropout(x, p=self.sample_dropout, training=self.training)
        x = self.fc2(x)
        x = F.dropout(x, p=self.sample_dropout, training=self.training)
        if self.scale:
            x = x * (self.super_mlp_ratio / self.sample_mlp_ratio)
        x = self.drop_path(x)
        x = residual + x
        x = self.maybe_layer_norm(self.ffn_layer_norm, x, after=True)
        # print("ffn :", time.time() - start_time)
        return x

    def maybe_layer_norm(self, layer_norm, x, before=False, after=False):
        assert before ^ after
        if after ^ self.normalize_before:
            return layer_norm(x)
        else:
            return x


@model_wrapper
class AutoformerSpace(nn.Module):
    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, embed_dim=256, depth=14,
                 num_heads=4, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., pre_norm=True, scale=False, gp=False, relative_position=False, change_qkv=False, abs_pos = True, max_relative_position=14,
                 search_embed_dim = [192,216,240], search_mlp_ratio=[3.5, 4.0], search_heads=[3, 4], search_depth=[12,13,14],
                 ):
        super().__init__()
        # the configs of super arch
        self.super_embed_dim = embed_dim
        self.super_mlp_ratio = mlp_ratio
        self.super_layer_num = depth
        self.super_num_heads = num_heads
        self.super_dropout = drop_rate
        self.super_attn_dropout = attn_drop_rate
        self.num_classes = num_classes
        self.pre_norm=pre_norm
        self.scale=scale

        sample_embed_dim = nn.ValueChoice(search_embed_dim)
        self.sample_depth = nn.ValueChoice(search_depth)

        self.patch_embed_super = nn.Conv2d(in_chans, sample_embed_dim, kernel_size=patch_size, stride=patch_size)                 
        self.gp = gp
        self.cls_token = nn.Parameter(torch.zeros(1, 1, sample_embed_dim))
        trunc_normal_(self.cls_token, std=.02)

        self.blocks = []
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        
        self.abs_pos = abs_pos
        if self.abs_pos:
            num_patches = (img_size // patch_size) ** 2
            self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, sample_embed_dim))
            trunc_normal_(self.pos_embed, std=.02)

        for i in range(depth):
            choices = []
            for x in itertools.product(search_mlp_ratio, search_heads):
                sample_mlp_ratio, sample_heads = x[0], x[1]
                choices.append(TransformerEncoderLayer(dim=sample_embed_dim, num_heads=sample_heads, mlp_ratio=sample_mlp_ratio,
                                                       qkv_bias=qkv_bias, qk_scale=qk_scale, dropout=drop_rate,
                                                       attn_drop=attn_drop_rate, drop_path=dpr[i],
                                                       pre_norm=pre_norm, scale=self.scale,
                                                       change_qkv=change_qkv, relative_position=relative_position,
                                                       max_relative_position=max_relative_position))
            choices = nn.LayerChoice(choices)
            self.blocks.append(choices)

        if self.pre_norm:
            self.norm = nn.LayerNorm(sample_embed_dim)
        self.head = nn.Linear(sample_embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward(self, x):
        B = x.shape[0]
        x = self.patch_embed_super(x)
        cls_tokens = self.cls_token.expand(B, -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)
        if self.abs_pos:
            x = x + self.pos_embed
        for i in range(self.sample_depth):
            x = self.blocks[i](x)
        if self.pre_norm:
            x = self.norm(x)
        if self.gp:
            x = torch.mean(x[:, 1:] , dim=1)
        else:
            x = x[:, 0]
        
        x = self.head(x)

        return x
                
