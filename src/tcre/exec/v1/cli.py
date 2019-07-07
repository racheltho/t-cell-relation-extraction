import os
import os.path as osp
import pathlib as pl
import pandas as pd
import numpy as np
import json
import shutil
import pprint
import warnings
from collections import defaultdict, Counter
from snorkel import SnorkelSession
from snorkel.models import Candidate, GoldLabel
from tcre.env import *
from tcre.supervision import get_candidate_classes, ENT_TYP_CT_L, ENT_TYP_CK_L, ENT_TYP_TF_L, SPLIT_DEV
from tcre.modeling import utils
from tcre.modeling import features
from tcre.modeling import data as tcre_data
from tcre.modeling.vocab import W2VVocab
from tcre.modeling.training import supervise, load_checkpoint, set_seed
from tcre.exec.v1.model import RERNN, DIST_PAD_VAL
from torchtext import data as txd
from torchtext.vocab import Vocab
from torchtext.data import BucketIterator, Iterator
import click
import logging
import dill
import torch
logger = logging.getLogger(__name__)

DEFAULT_SWAPS = {
    ENT_TYP_CT_L: 'CELL',
    ENT_TYP_CK_L: 'CYTOKINE',
    ENT_TYP_TF_L: 'TF'
}
MARKER_LISTS = {
    'doub_01': [('<<', '>>'), ('[[', ']]'), ('##', '##'), ('@@', '@@')],
    'sngl_01': [('<', '>'), ('[', ']'), ('#', '#'), ('@', '@')],
    'mult_01': [('< #', '# >'), ('< @', '@ >'), ('| #', '# |'), ('| @', '@')]
}
MODEL_SIZES = {
    'S': {'hidden_dim': 5, 'wrd_embed_dim': 10, 'pos_embed_dim': 3},
    'M': {'hidden_dim': 10, 'wrd_embed_dim': 30, 'pos_embed_dim': 8},
    'L': {'hidden_dim': 20, 'wrd_embed_dim': 50, 'pos_embed_dim': 10},
    'XL': {'hidden_dim': 30, 'wrd_embed_dim': 100, 'pos_embed_dim': 10},
}


def get_modeling_config(**kwargs):
    markers = MARKER_LISTS[kwargs['marker_list']]
    if not kwargs['use_secondary']:
        markers[2] = None
        markers[3] = None
    typ0, typ1 = kwargs['entity_types']
    markers = {
        'primary': {typ0: list(markers[0]), typ1: list(markers[1])},
        'secondary': {typ0: list(markers[2] or []), typ1: list(markers[3] or [])}
    }

    res = dict(kwargs)
    res['label'] = ':'.join([f'{k}={v}' for k, v in kwargs.items() if k != 'entity_types'])
    res['markers'] = markers
    res['swaps'] = DEFAULT_SWAPS if kwargs['use_swaps'] else None
    return res


def _splits(splits_file, keys=None):
    # Expect splits written using something like:
    # pd.Series({'train': [1,2,3], 'test': [3,4]}).to_json('/tmp/splits.json', orient='index')
    # --> {"train":[1,2,3],"test":[3,4]}

    logger.info(f'Gathering candidates for splits at "{splits_file}"')
    splits = pd.read_json(splits_file, typ='series', orient='index').to_dict()
    if keys is not None:
        splits = {k: v for k, v in splits.items() if k in keys}
    splits_ct = {s: len(cids) for s, cids in splits.items()}
    logger.info(f'Split sizes = {splits_ct}')
    return splits


def _cands(candidate_class, splits):
    session = SnorkelSession()
    cids = list(set([cid for s, cids in splits.items() for cid in cids]))
    return session.query(candidate_class.subclass) \
        .filter(candidate_class.subclass.id.in_(cids)).all()


def get_model(fields, config):
    model_args = dict(MODEL_SIZES[config['model_size']])
    for k in ['dropout', 'bidirectional', 'cell_type', 'device', 'num_layers', 'max_dist']:
        if k in config:
            model_args[k] = config[k]
    if config['wrd_embedding_type'] != 'denovo':
        model_args['wrd_embed_dim'] = None
    if config['wrd_embedding_type'] in ['w2v_trained']:
        model_args['train_wrd_embed'] = True
    if not config['use_positions']:
        model_args['pos_embed_dim'] = 0
    model = RERNN(fields, **model_args)
    return model, model_args


def _datasets(cands, splits, config, fields):

    logger.info('Collecting features')
    # Filter for candidates used in marking
    if config['use_secondary']:
        predicate = lambda e: e['type'] in config['entity_types']
    else:
        predicate = lambda e: e['is_candidate'] and e['type'] in config['entity_types']

    # Get marked token sequences with distance features (as sequences)
    records = features.candidates_to_records(cands, entity_predicate=predicate)
    df = features.get_record_features(
        records, markers=config['markers'], swaps=config['swaps'],
        lower=config['use_lower'],
        subtokenizer=lambda t: t.split(), assert_unique=False
    )
    df = df.rename(columns={'tokens': 'text'})
    if not df['label'].between(0, 1).all():
        ex = df['label'][~df['label'].between(0, 1)].unique()
        raise AssertionError(f"Found label values outside [0, 1]; Examples: {ex[:10]}")

    logger.info(f'Sample feature records:\n')
    for r in df.head(5).to_dict(orient='records'):
        logger.info(pprint.pformat(r, width=128, compact=True, indent=6))
    logger.info('Label distribution:\n%s',
                pd.concat([df['label'].value_counts(), df['label'].value_counts(normalize=True)], axis=1))

    # Build dataset for each split (one candidate may exist in multiple splits)
    logger.info('Initializing batch iterators')
    dfi = df.set_index('id', drop=False)
    datasets = pd.DataFrame([
        dfi.loc[cid].append(pd.Series({'split': s}))
        for s, cids in splits.items() for cid in cids
    ]).reset_index(drop=True)
    datasets = {
        k: tcre_data.DataFrameDataset(g.drop('split', axis=1), fields)
        for k, g in datasets.groupby('split')
    }

    return datasets


def _predict(cands, splits, config):
    """Predict labels for candidates using a model checkpoint"""

    output_dir = pl.Path(config['output_dir'])

    # Load model weights
    checkpoint_dir = output_dir / 'checkpoints'
    logger.info(f'Loading model state from checkpoint dir {checkpoint_dir}')
    checkpoint = load_checkpoint(checkpoint_dir)

    # Load fields
    with (output_dir / 'fields.pkl').open('rb') as f:
        fields = dill.load(f)

    model, model_args = get_model(fields, config)
    model.load_state_dict(checkpoint['model'])
    logger.info(f'Restored model with arguments: {model_args}')

    datasets = _datasets(cands, splits, config, fields)
    if 'predict' not in datasets:
        raise AssertionError(f'Expecting dataset with key "predict"; got datasets {datasets.keys()}')
    dataset = datasets['predict']

    iterator = Iterator(
        dataset,
        config['batch_size'],
        sort_key=lambda x: len(x.text),
        sort_within_batch=True,
        repeat=False,
        shuffle=False,
        device=config['device']
    )

    model = model.to(config['device'])
    model.eval()
    with torch.no_grad():
        predictions = []
        for batch in iterator:
            predictions.append(pd.DataFrame({
                'id': batch.id.clone().cpu().numpy(),
                'y_true': batch.label.clone().cpu().numpy(),
                'y_pred': model.predict(batch).clone().cpu().numpy()
            }))
        predictions = pd.concat(predictions).reset_index(drop=True)

    return predictions


def _train(cands, splits, config):
    """Train a single model

    Args:
        cands: List of all candidates
        splits: Dict with keys "train", "val", "test" each having a list of integer candidate ids
        config: Training configuration
    """
    set_seed(config['seed'])
    SEQ_LEN = 128

    # Check that required keys are present in splits
    if len({'train', 'val', 'test'} - set(splits.keys())) > 0:
        raise ValueError(f'Splits must contain keys "train", "val", "test", got keys {splits.keys()}')

    fields = {
        'text': txd.Field(sequential=True, lower=False, fix_length=SEQ_LEN, include_lengths=True),
        'label': txd.Field(sequential=False, use_vocab=False),
        'e0_dist': txd.Field(sequential=True, use_vocab=False, pad_token=DIST_PAD_VAL, fix_length=SEQ_LEN),
        'e1_dist': txd.Field(sequential=True, use_vocab=False, pad_token=DIST_PAD_VAL, fix_length=SEQ_LEN),
        'id': txd.Field(sequential=False, use_vocab=False)
    }

    datasets = _datasets(cands, splits, config, fields)

    # If using w2v, set text field vocabulary on all datasets
    if config['wrd_embedding_type'].startswith('w2v'):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message='.*')
            from gensim.models import KeyedVectors
            logger.info(f"Loading w2v model with vocab limit {config['vocab_limit']}")
            w2v = KeyedVectors.load_word2vec_format(W2V_MODEL_01, binary=True, limit=config['vocab_limit'])
            fields['text'].vocab = W2VVocab(w2v)
    # Build vocab on training dataset text field ONLY and assign to others
    elif config['wrd_embedding_type'] == 'denovo':
        fields['text'].build_vocab(datasets['train'])
    # Otherwise, embedding type is not valid
    else:
        raise ValueError(f"Word embedding type {config['wrd_embedding_type']} not valid")

    # Split the dataset and build iterators
    train_iter, val_iter, test_iter = BucketIterator.splits(
        tuple([datasets[k] for k in ['train', 'val', 'test']]),
        batch_sizes=[config['batch_size']]*3,
        sort_key=lambda x: len(x.text),
        sort=True,
        sort_within_batch=True,
        repeat=False
    )
    logger.info('Split datasets with sizes %s', {k: len(ds) for k, ds in datasets.items()})

    model, model_args = get_model(fields, config)
    config['model_args'] = model_args
    logger.info(f"Built model with arguments: {config['model_args']}")
    lr, decay = config['learning_rate'], config['weight_decay']

    logger.info('Running optimization')
    history = supervise(
        model, lr, decay, train_iter, val_iter, test_iter=test_iter,
        model_dir=config['output_dir'] if config['use_checkpoints'] else None,
        seed=config['seed']
    )
    return history, fields


def _to_candidate_class(relation_class):
    classes = get_candidate_classes()
    classes = {classes[c].field: classes[c] for c in classes}
    return classes[relation_class]


PARAMS = {}


class param(object):
    """Decorator for click.option used to register parameter names for dynamic requirements"""

    def __init__(self, *args, **kwargs):
        self.param = args[0].replace('--', '').replace('-', '_')
        self.click_fn = click.option(*args, **kwargs)

    def __call__(self, f):
        self.fn_name = f.__name__
        if self.fn_name not in PARAMS:
            PARAMS[self.fn_name] = []
        PARAMS[self.fn_name].append(self.param)
        return self.click_fn(f)


@click.group(invoke_without_command=True)
@param('--relation-class', default=None, required=True, help='Candidate type class (e.g. "inducing_cytokine")')
@param('--device', default='cuda', required=True, help='Device to use for training')
@param('--batch-size', default=128, required=True, help='Batch size used in training and prediction')
@param('--output-dir', default=None, required=True, help='Output directory (nothing saved if omitted)')
@param('--seed', default=TCRE_SEED, required=True, help='RNG seed')
@param('--log-level', default='info', help='Logging level')
@click.pass_context
def cli(ctx, relation_class, device, batch_size, output_dir, seed, log_level):
    logging.basicConfig(level=log_level.upper())
    ctx.obj['relation_class'] = relation_class
    ctx.obj['device'] = device
    ctx.obj['batch_size'] = batch_size
    ctx.obj['output_dir'] = output_dir
    ctx.obj['seed'] = seed


@cli.command()
@param('--splits-file', default=None, required=True,
              help='Path to json file containing candidate ids keyed by split name ("train", "val", "test")')
@param('--marker-list', default='mult_01', required=True, help='Marker list name ("doub_01", "sngl_01")')
@param('--use-checkpoints', default=False, required=True, type=bool, help='Save checkpoint for best model')
@param('--use-secondary', default=True, required=True, type=bool, help='Use secondary markers')
@param('--use-swaps', default=True, required=True, type=bool, help='Use swaps for primary entity text')
@param('--use-lower', default=False, required=True, type=bool,
              help='Whether or not to use only lower case tokens')
@param('--use-positions', default=False, required=True, type=bool,
              help='Whether or not to use positional features')
@param('--wrd-embedding-type', default='w2v_frozen', required=True,
              help='One of "w2v_frozen", "w2v_trained", or "denovo"')
@param('--vocab-limit', default=50000, required=True, help='For pre-trained vectors, max vocab size')
@param('--model-size', default='S', help='One of "S", "M", "L"')
@param('--bidirectional', default=False, type=bool, help='Use bi-directional RNN')
@param('--cell-type', default='LSTM', help='LSTM or GRU')
@param('--weight-decay', default=0.0, help='Weight decay for training')
@param('--dropout', default=0.0, help='Dropout rate')
@param('--learning-rate', default=.005, help='Learning rate')
@param('--save-keys', default='history,config,fields',
              help='Resulting data to save as csv list (output_dir must be set to have an effect)')
@click.pass_context
def train(ctx, splits_file, marker_list, use_checkpoints, use_secondary, use_swaps, use_lower, use_positions,
        wrd_embedding_type, vocab_limit, model_size, bidirectional, cell_type,
        weight_decay, dropout, learning_rate, save_keys):
    relation_class = ctx.obj['relation_class']
    candidate_class = _to_candidate_class(relation_class)
    output_dir = ctx.obj['output_dir']

    splits = _splits(splits_file, keys=['train', 'val', 'test'])
    cands = _cands(candidate_class, splits)
    logger.info(f'Found {len(cands)} candidates')

    config = get_modeling_config(
        relation_class=relation_class,
        splits_file=splits_file,
        entity_types=candidate_class.entity_types,
        marker_list=marker_list,
        use_secondary=use_secondary,
        use_swaps=use_swaps,
        use_lower=use_lower,
        use_positions=use_positions,
        use_checkpoints=use_checkpoints,
        wrd_embedding_type=wrd_embedding_type,
        model_size=model_size,
        bidirectional=bidirectional,
        cell_type=cell_type,
        learning_rate=learning_rate,
        weight_decay=weight_decay,
        dropout=dropout,
        vocab_limit=vocab_limit,
        save_keys=save_keys,
        device=ctx.obj['device'],
        batch_size=ctx.obj['batch_size'],
        output_dir=ctx.obj['output_dir'],
        seed=ctx.obj['seed']
    )
    logger.info(f'Modeling config:\n{pprint.pformat(config, compact=True, indent=6, width=128)}')

    # Clear the output directory if present
    if output_dir is not None:
        logger.info(f'Initializing {output_dir}')
        output_dir = pl.Path(output_dir)
        if output_dir.exists():
            # Validate that path is at least 2 levels away from root
            # dir before deleting it
            if len(output_dir.resolve().parts) <= 2:
                raise AssertionError(
                    f'Path "{output_dir}" is too close to root dir to delete '
                    f'(are you sure this is correct)?')
            shutil.rmtree(output_dir)
        if not output_dir.exists():
            output_dir.mkdir(parents=True)

    history, fields = _train(cands, splits, config)

    if output_dir is not None:
        save_keys = [v.strip().lower() for v in save_keys.split(',')]
        if 'fields' in save_keys:
            logger.info('Saving input fields definition to fields.pkl')
            with (output_dir / 'fields.pkl').open('wb') as f:
                dill.dump(fields, f)
        if 'history' in save_keys:
            logger.info('Saving history to history.json')
            with (output_dir / 'history.json').open('w') as f:
                json.dump(history, f)
        if 'config' in save_keys:
            logger.info('Saving config to config.json')
            with (output_dir / 'config.json').open('w') as f:
                json.dump(config, f)

    logger.info('Training complete')


@cli.command()
@param('--splits-file', default=None, help='Path to json file containing candidate ids keyed by split name ("predict")')
@click.pass_context
def predict(ctx, splits_file):
    candidate_class = _to_candidate_class(ctx.obj['relation_class'])
    output_dir = ctx.obj['output_dir']

    # Read in configuration from training and overwrite any non-essential properties
    config = json.loads((pl.Path(output_dir) / 'config.json').read_text('utf-8'))
    for prop in ['device', 'batch_size']:
        config[prop] = ctx.obj[prop]

    splits = _splits(splits_file, keys=['predict'])
    cands = _cands(candidate_class, splits)
    logger.info(f'Found {len(cands)} candidates')

    logger.info('Gathering predictions')
    predictions = _predict(cands, splits, config)
    logger.info(f'Prediction Info:\n{predictions.info()}')

    path = pl.Path(output_dir) / 'predictions.json'
    logger.info(f'Saving predictions to {path}')
    predictions.to_json(path)


if __name__ == '__main__':
    cli(obj={})
