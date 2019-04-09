from __future__ import print_function

import json
import random
import os
import argparse

import numpy as np
import pandas as pd
import tensorflow as tf

from .util.eval_utils import compute_f1, compute_hits
from .config import get_config, DEFAULT_CONFIG
from .data_utils import build_vocabulary, tokenize_utterance, vectorize_sequences
from .learning_to_rank import (make_dataset,
                               load,
                               save_vocabulary,
                               create_model_personachat,
                               train,
                               predict,
                               get_optimizer)

random.seed(273)
np.random.seed(273)
tf.set_random_seed(273)

MODEL_FILENAME = 'learning_to_rank.ckpt'
CONFIG = get_config(os.path.join(os.path.dirname(__file__), DEFAULT_CONFIG))


def make_dataset(in_table, in_rev_vocab, config, use_sample_weights=True):
    # n lists (#context_turns) of lists
    questions_tokenized = [[] for _ in range(config['max_context_turns'])]
    for persona_turns, context_turns in zip(in_table.persona, in_table.context):
        all_turns = (persona_turns + context_turns)[:config['max_context_turns']]
        context_turns_padded = ['' for _ in range((config['max_context_turns'] - len(all_turns) ))] + all_turns
        for turn_idx, turn in enumerate(context_turns_padded):
            questions_tokenized[turn_idx].append(tokenize_utterance(turn,
                                                                    remove_stopwords=False,
                                                                    add_special_symbols=False))
    responses_tokenized = []
    for response in in_table.response:
        responses_tokenized.append(tokenize_utterance(response, remove_stopwords=False, add_special_symbols=False))

    questions_vectorized = []
    for turns_list in questions_tokenized:
        questions_vectorized.append(vectorize_sequences(turns_list, in_rev_vocab))
    responses_vectorized = vectorize_sequences(responses_tokenized, in_rev_vocab)
    questions_padded = [tf.keras.preprocessing.sequence.pad_sequences(questions_list, maxlen=config['max_sequence_length'])
                        for questions_list in questions_vectorized]
    responses_vectorized = tf.keras.preprocessing.sequence.pad_sequences(responses_vectorized, maxlen=config['max_sequence_length'])

    targets = np.expand_dims(in_table.target, -1)

    X = list(map(np.asarray,
                 questions_padded + [responses_vectorized,
                                     [sent for sent in in_table.c_sentiment],
                                     [sent for sent in in_table.a_sentiment]]))
    if not use_sample_weights:
        return X, targets, np.expand_dims(np.ones(len(responses_vectorized)), -1)
    default_weight = config['bot_sample_weights']['default']
    X_weight = np.asarray([default_weight for _ in range(len(in_table['bot']))])
    for index, bot in enumerate(in_table['bot']):
        for bot_prefix, weight in CONFIG['bot_sample_weights'].iteritems():
            X_weight[index] = weight
            break
    return X, targets, X_weight


def make_training_data(in_train, in_dev, in_test, in_sample_weight, in_config):
    utterances_tokenized = []
    for context_utterances in in_train.context:
        utterances_tokenized += [tokenize_utterance(utt, add_special_symbols=False, remove_stopwords=False)
                                 for utt in context_utterances]
    utterances_tokenized += list(map(lambda x: tokenize_utterance(x, add_special_symbols=False, remove_stopwords=False),
                                     in_train.response))

    word_vocab, rev_word_vocab = build_vocabulary(in_train.context.values.tolist() + in_train.response.values.tolist(),
                                                  max_size=in_config['max_vocab_size'])

    in_config['vocab_size'] = len(rev_word_vocab)
    X, y, X_weight = make_dataset(in_train, rev_word_vocab, in_config, use_sample_weights=in_sample_weight)
    X_dev, y_dev, X_dev_weight = make_dataset(in_dev,
                                              rev_word_vocab,
                                              in_config,
                                              use_sample_weights=in_sample_weight)
    X_test, y_test, X_test_weight = make_dataset(in_test,
                                                 rev_word_vocab,
                                                 in_config,
                                                 use_sample_weights=in_sample_weight)
    return ((X, y, X_weight),
            (X_dev, y_dev, X_dev_weight),
            (X_test, y_test, X_test_weight),
            rev_word_vocab)


def evaluate_personachat(model, eval_set, config):
    X_true, y_true, X_true_w = make_dataset(eval_set, rev_vocab, config, use_sample_weights=False)

    pred = predict(model, X_true)
    pred = pred.reshape((None, 20))

    hits_at_1, f1 = compute_hits(pred, pred), compute_f1(pred, pred)
    print('Hits@1: {:.3f} F1: {:.3f}'.format(hits_at_1, f1))


def build_argument_parser():
    result = argparse.ArgumentParser()
    result.add_argument('trainset')
    result.add_argument('devset')
    result.add_argument('testset')
    result.add_argument('model_folder')
    result.add_argument('--bot_sample_weight', action='store_true')
    result.add_argument('--config', default=os.path.join(os.path.dirname(__file__), DEFAULT_CONFIG))
    result.add_argument('--evaluate', action='store_true', default=False)
    result.add_argument('--candidates_number', default=20, type=int)
    return result


if __name__ == '__main__':
    parser = build_argument_parser()
    args = parser.parse_args()
    trainset = pd.read_json(args.trainset).sample(frac=1).reset_index(drop=True)
    devset = pd.read_json(args.devset)
    testset = pd.read_json(args.testset)

    with tf.Session() as sess:
        if args.evaluate:
            model, config, _ = load(args.model_folder, sess)
            evaluate_personachat(model, testset, config)
        else:
            CONFIG = get_config(args.config)

            train_data, dev_data, test_data, rev_vocab = make_training_data(trainset,
                                                                            devset,
                                                                            testset,
                                                                            args.bot_sample_weight,
                                                                            CONFIG)
            X, y, X_w = train_data
            X_dev, y_dev, X_dev_w = dev_data
            X_test, y_test, X_test_w = test_data

            if not os.path.exists(args.model_folder):
                os.makedirs(args.model_folder)

            save_vocabulary(rev_vocab, os.path.join(args.model_folder, 'rev_vocab'))
            with open(os.path.join(args.model_folder, 'config.json'), 'w') as config_out:
                json.dump(CONFIG, config_out)

            opt = get_optimizer(sess, **CONFIG)
            print('Training with config "{}" :'.format(args.config))
            print(json.dumps(CONFIG, indent=2))
            model = create_model_personachat(**CONFIG)
            checkpoint_file = os.path.join(args.model_folder, MODEL_FILENAME)
            train(model,
                  (X, y, X_w),
                  (X_dev, y_dev, X_dev_w),
                  (X_test, y_test, X_test_w),
                  opt,
                  checkpoint_file,
                  sess,
                  **CONFIG)
