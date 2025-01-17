import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from fairseq import options, utils
from fairseq.models.transformer import TransformerModel, TransformerEncoder, TransformerDecoder, base_architecture
from fairseq.models import (
    FairseqEncoder,
    FairseqIncrementalDecoder,
    register_model,
    register_model_architecture,
)
from fairseq.modules import (
    LayerDropModuleList,
)
from .modules.checkpoint_activations import checkpoint_wrapper
from fairseq.modules.quant_noise import quant_noise as apply_quant_noise_
from .modules.prime_transformer_layer import PrimeTransformerEncoderLayer, PrimeTransformerDecoderLayer


DEFAULT_MAX_SOURCE_POSITIONS = 1024
DEFAULT_MAX_TARGET_POSITIONS = 1024


@register_model("prime_transformer")
class PrimeTransformerModel(TransformerModel):
    """
    Transformer model from `"Attention Is All You Need" (Vaswani, et al, 2017)
    <https://arxiv.org/abs/1706.03762>`_.
    Args:
        encoder (TransformerEncoder): the encoder
        decoder (TransformerDecoder): the decoder
    The Transformer model provides the following named architectures and
    command-line arguments:
    .. argparse::
        :ref: fairseq.models.transformer_parser
        :prog:
    """

    def __init__(self, args, encoder, decoder):
        super().__init__(args, encoder, decoder)

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        # fmt: off  # TODO
        parser.add_argument('--activation-fn',
                            choices=utils.get_available_activation_fns(),
                            help='activation function to use')
        parser.add_argument('--dropout', type=float, metavar='D',
                            help='dropout probability')
        parser.add_argument('--attention-dropout', type=float, metavar='D',
                            help='dropout probability for attention weights')
        parser.add_argument('--activation-dropout', '--relu-dropout', type=float, metavar='D',
                            help='dropout probability after activation in FFN.')
        parser.add_argument('--encoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained encoder embedding')
        parser.add_argument('--encoder-embed-dim', type=int, metavar='N',
                            help='encoder embedding dimension')
        parser.add_argument('--encoder-ffn-embed-dim', type=int, metavar='N',
                            help='encoder embedding dimension for FFN')
        parser.add_argument('--encoder-layers', type=int, metavar='N',
                            help='num encoder layers')
        parser.add_argument('--encoder-attention-heads', type=int, metavar='N',
                            help='num encoder attention heads')
        parser.add_argument('--encoder-normalize-before', action='store_true',
                            help='apply layernorm before each encoder block')
        parser.add_argument('--encoder-learned-pos', action='store_true',
                            help='use learned positional embeddings in the encoder')
        parser.add_argument('--decoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained decoder embedding')
        parser.add_argument('--decoder-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension')
        parser.add_argument('--decoder-ffn-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension for FFN')
        parser.add_argument('--decoder-layers', type=int, metavar='N',
                            help='num decoder layers')
        parser.add_argument('--decoder-attention-heads', type=int, metavar='N',
                            help='num decoder attention heads')
        parser.add_argument('--decoder-learned-pos', action='store_true',
                            help='use learned positional embeddings in the decoder')
        parser.add_argument('--decoder-normalize-before', action='store_true',
                            help='apply layernorm before each decoder block')
        parser.add_argument('--decoder-output-dim', type=int, metavar='N',
                            help='decoder output dimension (extra linear layer '
                                 'if different from decoder embed dim')
        parser.add_argument('--share-decoder-input-output-embed', action='store_true',
                            help='share decoder input and output embeddings')
        parser.add_argument('--share-all-embeddings', action='store_true',
                            help='share encoder, decoder and output embeddings'
                                 ' (requires shared dictionary and embed dim)')
        parser.add_argument('--no-token-positional-embeddings', default=False, action='store_true',
                            help='if set, disables positional embeddings (outside self attention)')
        parser.add_argument('--adaptive-softmax-cutoff', metavar='EXPR',
                            help='comma separated list of adaptive softmax cutoff points. '
                                 'Must be used with adaptive_loss criterion'),
        parser.add_argument('--adaptive-softmax-dropout', type=float, metavar='D',
                            help='sets adaptive softmax dropout for the tail projections')
        parser.add_argument('--layernorm-embedding', action='store_true',
                            help='add layernorm to embedding')
        parser.add_argument('--no-scale-embedding', action='store_true',
                            help='if True, dont scale embeddings')
        parser.add_argument('--checkpoint-activations', action='store_true',
                            help='checkpoint activations at each layer, which saves GPU '
                                 'memory usage at the cost of some additional compute')
        # args for "Cross+Self-Attention for Transformer Models" (Peitz et al., 2019)
        parser.add_argument('--no-cross-attention', default=False, action='store_true',
                            help='do not perform cross-attention')
        parser.add_argument('--cross-self-attention', default=False, action='store_true',
                            help='perform cross+self-attention')
        # args for "Reducing Transformer Depth on Demand with Structured Dropout" (Fan et al., 2019)
        parser.add_argument('--encoder-layerdrop', type=float, metavar='D', default=0,
                            help='LayerDrop probability for encoder')
        parser.add_argument('--decoder-layerdrop', type=float, metavar='D', default=0,
                            help='LayerDrop probability for decoder')
        parser.add_argument('--encoder-layers-to-keep', default=None,
                            help='which layers to *keep* when pruning as a comma-separated list')
        parser.add_argument('--decoder-layers-to-keep', default=None,
                            help='which layers to *keep* when pruning as a comma-separated list')
        # args for Training with Quantization Noise for Extreme Model Compression ({Fan*, Stock*} et al., 2020)
        parser.add_argument('--quant-noise-pq', type=float, metavar='D', default=0,
                            help='iterative PQ quantization noise at training time')
        parser.add_argument('--quant-noise-pq-block-size', type=int, metavar='D', default=8,
                            help='block size of quantization noise at training time')
        parser.add_argument('--quant-noise-scalar', type=float, metavar='D', default=0,
                            help='scalar quantization noise and scalar quantization at training time')
        # for prime
        parser.add_argument('--use_att', type=str, nargs='+',
                            default=['es', 'ds', 'dc', ],
                            help='')
        parser.add_argument('--kernel_size', type=int, default=0, help='do not set static kernel')
        parser.add_argument('--attn_dynamic_type', type=int, default=0,
                            help='0: no use,1 use static kernel(k>0) or depth kernel(k==0) 2. use  dynamic kernel ')
        parser.add_argument('--attn_cat_relu', type=int, default=0)
        parser.add_argument('--attn_wide_kernels', type=lambda x: options.eval_str_list(x, int),
                            help='list of kernel size (default: "[3,15]") for wide and gate')
        parser.add_argument('--weight-dropout', type=float, metavar='D',
                            help='dropout probability for conv weights')
        parser.add_argument('--dynamic_gate', type=int, default=1, help='0,1')
        parser.add_argument('--dynamic_depth_kernels', type=lambda x: options.eval_str_list(x, int),
                            help='list of kernel size (default: "[3,3,3,7,7,7,7,7,7,15,15,15]"),for ffn or attn')
        parser.add_argument('--dynamic_padding', type=int, default=0, help='padding before dynamic conv')
        parser.add_argument('--attn_dynamic_cat', type=int, default=1)
        parser.add_argument('--input_dropout', type=float, default=0, help='')
        parser.add_argument('--init_method', type=str, default='km',
                            help='xavier,km，xi,fixup')
        parser.add_argument('--lnv', type=str, default='origin',
                            help='layernorm,adanorm')
        # fmt: on

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure all arguments are present in older models
        prime_base_architecture(args)

        if args.encoder_layers_to_keep:
            args.encoder_layers = len(args.encoder_layers_to_keep.split(","))
        if args.decoder_layers_to_keep:
            args.decoder_layers = len(args.decoder_layers_to_keep.split(","))

        if getattr(args, "max_source_positions", None) is None:
            args.max_source_positions = DEFAULT_MAX_SOURCE_POSITIONS
        if getattr(args, "max_target_positions", None) is None:
            args.max_target_positions = DEFAULT_MAX_TARGET_POSITIONS

        src_dict, tgt_dict = task.source_dictionary, task.target_dictionary

        if args.share_all_embeddings:
            if src_dict != tgt_dict:
                raise ValueError("--share-all-embeddings requires a joined dictionary")
            if args.encoder_embed_dim != args.decoder_embed_dim:
                raise ValueError(
                    "--share-all-embeddings requires --encoder-embed-dim to match --decoder-embed-dim"
                )
            if args.decoder_embed_path and (
                args.decoder_embed_path != args.encoder_embed_path
            ):
                raise ValueError(
                    "--share-all-embeddings not compatible with --decoder-embed-path"
                )
            encoder_embed_tokens = cls.build_embedding(
                args, src_dict, args.encoder_embed_dim, args.encoder_embed_path
            )
            decoder_embed_tokens = encoder_embed_tokens
            args.share_decoder_input_output_embed = True
        else:
            encoder_embed_tokens = cls.build_embedding(
                args, src_dict, args.encoder_embed_dim, args.encoder_embed_path
            )
            decoder_embed_tokens = cls.build_embedding(
                args, tgt_dict, args.decoder_embed_dim, args.decoder_embed_path
            )

        encoder = cls.build_encoder(args, src_dict, encoder_embed_tokens)
        decoder = cls.build_decoder(args, tgt_dict, decoder_embed_tokens)
        return cls(args, encoder, decoder)

    @classmethod
    def build_encoder(cls, args, src_dict, embed_tokens):
        return PrimeTransformerEncoder(args, src_dict, embed_tokens)

    @classmethod
    def build_decoder(cls, args, tgt_dict, embed_tokens):
        return PrimeTransformerDecoder(
            args,
            tgt_dict,
            embed_tokens,
            no_encoder_attn=getattr(args, "no_cross_attention", False),
        )


class PrimeTransformerEncoder(TransformerEncoder):
    """
    Transformer encoder consisting of *args.encoder_layers* layers. Each layer
    is a :class:`TransformerEncoderLayer`.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        dictionary (~fairseq.data.Dictionary): encoding dictionary
        embed_tokens (torch.nn.Embedding): input embedding
    """

    def __init__(self, args, dictionary, embed_tokens):
        super().__init__(args, dictionary=dictionary, embed_tokens=embed_tokens)
        if self.encoder_layerdrop > 0.0:
            self.layers = LayerDropModuleList(p=self.encoder_layerdrop)
        else:
            self.layers = nn.ModuleList([])
        self.layers.extend(
            [self.build_encoder_layer(args, i) for i in range(args.encoder_layers)]
        )

    def build_encoder_layer(self, args, layer_id=0):
        layer = PrimeTransformerEncoderLayer(args, layer_id=layer_id)
        if getattr(args, "checkpoint_activations", False):
            layer = checkpoint_wrapper(layer)
        return layer


class PrimeTransformerDecoder(TransformerDecoder):
    """
    Transformer decoder consisting of *args.decoder_layers* layers. Each layer
    is a :class:`TransformerDecoderLayer`.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        dictionary (~fairseq.data.Dictionary): decoding dictionary
        embed_tokens (torch.nn.Embedding): output embedding
        no_encoder_attn (bool, optional): whether to attend to encoder outputs
            (default: False).
        final_norm (bool, optional): apply layer norm to the output of the
            final decoder layer (default: True).
    """

    def __init__(self, args, dictionary, embed_tokens, no_encoder_attn=False):
        self.args = args
        super().__init__(args, dictionary=dictionary, embed_tokens=embed_tokens, no_encoder_attn=no_encoder_attn)
        if self.decoder_layerdrop > 0.0:
            self.layers = LayerDropModuleList(p=self.decoder_layerdrop)
        else:
            self.layers = nn.ModuleList([])
        self.layers.extend(
            [
                self.build_decoder_layer(args, no_encoder_attn, i)
                for i in range(args.decoder_layers)
            ]
        )

    def build_decoder_layer(self, args, no_encoder_attn=False, layer_id=0):
        layer = PrimeTransformerDecoderLayer(args, no_encoder_attn=no_encoder_attn, layer_id=layer_id)
        if getattr(args, "checkpoint_activations", False):
            layer = checkpoint_wrapper(layer)
        return layer


@register_model_architecture("prime_transformer", "prime_transformer")
def prime_base_architecture(args):
    base_architecture(args)
    args.use_att = getattr(args, "use_att", ['es', 'ds', 'dc', ])
    args.kernel_size = getattr(args, "kernel_size", 0)
    args.attn_dynamic_type = getattr(args, "attn_dynamic_type", 0)
    args.attn_cat_relu = getattr(args, "attn_cat_relu", 0)
    args.attn_wide_kernels = getattr(args, "attn_wide_kernels", [3,15])
    args.dynamic_gate = getattr(args, "dynamic_gate", 1)
    args.dynamic_depth_kernels = getattr(args, "dynamic_depth_kernels", [3, 3, 3, 7, 7, 7, 7, 7, 7, 15, 15, 15])
    args.dynamic_padding = getattr(args, "dynamic_padding", 0)
    args.attn_dynamic_cat = getattr(args, "attn_dynamic_cat", 1)
    args.input_dropout = getattr(args, "input_dropout", 0)
    args.init_method = getattr(args, "init_method", 'km')
    args.lnv = getattr(args, "lnv", 'origin')