import json
import random
from os import path, makedirs
import argparse

import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow.contrib import rnn

from config import get_config, DEFAULT_CONFIG
from data_utils import build_vocabulary, tokenize_utterance, vectorize_sequences
from util.training_utils import get_loss_function, batch_generator

random.seed(273)
np.random.seed(273)
tf.set_random_seed(273)

MODEL_FILENAME = 'learning_to_rank.ckpt'
TF_GRAPH = None
CONFIG = get_config(path.join(path.dirname(__file__), DEFAULT_CONFIG))


def flatten(l):
    return [item for sublist in l for item in sublist]


def create_model(sentiment_features_number,
                 max_context_turns,
                 max_sequence_length,
                 embedding_size,
                 vocab_size,
                 rnn_cell,
                 bidirectional,
                 dropout_prob,
                 mlp_sizes,
                 **kwargs):
    with tf.variable_scope('model', reuse=tf.AUTO_REUSE):
        X_context = [tf.placeholder(tf.int32,
                                    [None, max_sequence_length],
                                    name='X_context_{}'.format(i))
                     for i in xrange(max_context_turns)]
        X_answer = tf.placeholder(tf.int32,
                                  [None, max_sequence_length],
                                  name='X_answer')
        X_question_sentiment = tf.placeholder(tf.float32,
                                              [None, sentiment_features_number],
                                              name='X_context_sentiment')
        X_answer_sentiment = tf.placeholder(tf.float32,
                                            [None, sentiment_features_number],
                                            name='X_answer_sentiment')
        X_timestamp_hour = tf.placeholder(tf.float32, [None, 1], name='X_timestamp')
        X_bot_overlap = tf.placeholder(tf.float32, [None, 1], name='X_bot_overlap')

        X_mlp_inputs = [X_question_sentiment, X_answer_sentiment, X_timestamp_hour, X_bot_overlap]
        y = tf.placeholder(tf.float32, [None, 1], name='y')

        embeddings = tf.Variable(tf.random_uniform([vocab_size, embedding_size], -1.0, 1.0),
                                 name='emb')

        rnn_cell_class = getattr(rnn, rnn_cell)
        encoder = rnn_cell_class(embedding_size, name='encoder')
        if bidirectional:
            pass
        context_encodings = []
        for context_turn in X_context:
            turn_embedding = tf.nn.embedding_lookup(embeddings, context_turn)
            outputs, _ = tf.nn.dynamic_rnn(encoder, turn_embedding, dtype=tf.float32)
            context_encodings.append(outputs[:, -1, :])
        context_encoding = tf.add_n(context_encodings)
        answer_embedding = tf.nn.embedding_lookup(embeddings, X_answer)
        outputs, _ = tf.nn.dynamic_rnn(encoder, answer_embedding, dtype=tf.float32)
        answer_encoding = outputs[:, -1, :]
        all_input = tf.concat([context_encoding] + [answer_encoding], -1)

        W_context_answer = tf.Variable(tf.random_normal([2 * embedding_size, 128]),
                                       name='W_context_answer')
        b_context_answer = tf.Variable(tf.random_normal([128]), name='bias_context_answer')

        W_pred = tf.Variable(tf.random_normal([128 + sum([int(tensor.shape[-1])
                                                          for tensor in X_mlp_inputs]), 1]),
                             name='W_pred')
        b_pred = tf.Variable(tf.random_normal([1]), name='bias_pred')
        context_answer = tf.add(tf.matmul(all_input, W_context_answer), b_context_answer)

        all_mlp_input = tf.concat([context_answer] + X_mlp_inputs, -1)

        final_pred = tf.sigmoid(tf.add(tf.matmul(all_mlp_input, W_pred), b_pred))

        return X_context + [X_answer] + X_mlp_inputs, final_pred, y


def train(in_model,
          train_data,
          dev_data,
          test_data,
          in_checkpoint_filepath,
          session,
          epochs=CONFIG['epochs'],
          batch_size=CONFIG['batch_size'],
          sample_weights=None,
          lr=0.001,
          l2_coef=0.0,
          lr_decay=1.0,
          early_stopping_threshold=100,
          optimizer='GradientDescentOptimizer',
          **kwargs):
    X, pred, y = in_model
    X_train, y_train, X_weights = train_data
    X_dev, y_dev, X_dev_weights = dev_data
    X_test, y_test, X_test_weights, = test_data

    if sample_weights is None:
        sample_weights = np.expand_dims(np.ones(y_train.shape[0]), -1)
    with tf.variable_scope('model', reuse=tf.AUTO_REUSE):
        batch_sample_weight = tf.placeholder(tf.float32, [None, 1], name='sample_weight')

    # Define loss and optimizer
    loss_op = get_loss_function(pred, y, batch_sample_weight, l2_coef=l2_coef)

    global_step = tf.Variable(0, trainable=False)
    session.run(tf.assign(global_step, 0))
    learning_rate = tf.train.cosine_decay(lr, global_step, 2000000, alpha=0.001)
    optimizer_class = getattr(tf.train, optimizer)
    optimizer = optimizer_class(learning_rate=learning_rate)
    train_op = optimizer.minimize(loss_op, global_step)

    saver = tf.train.Saver(tf.global_variables())

    epochs_without_improvement = 0
    best_loss, best_loss_step = np.inf, 0
    for epoch_counter in xrange(epochs):
        batch_gen = batch_generator(X_train, y_train, sample_weights, batch_size)
        train_batch_losses = []
        for batch_x, batch_y, batch_w in batch_gen:
            feed_dict = {X_i: batch_x_i for X_i, batch_x_i in zip(X, batch_x)}
            feed_dict.update({y: batch_y, batch_sample_weight: batch_w})
            _, train_batch_loss = session.run([train_op, loss_op],
                                              feed_dict=feed_dict)
            train_batch_losses.append(train_batch_loss)
        dev_eval_loss = evaluate_loss(in_model,
                                      dev_data,
                                      session)
        print 'Epoch {} out of {} results'.format(epoch_counter, epochs)
        print 'train loss: {:.3f}'.format(np.mean(train_batch_losses))
        print 'dev loss: {:.3f}'.format(dev_eval_loss) + ' @lr={}'.format(session.run(learning_rate))
        if dev_eval_loss < best_loss:
            best_loss = dev_eval_loss
            saver.save(session, in_checkpoint_filepath)
            print 'New best loss. Saving checkpoint'
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
        if early_stopping_threshold < epochs_without_improvement:
            print 'Early stopping after {} epochs'.format(epoch_counter)
            break


def evaluate_loss(in_model,
                  test_data,
                  session,
                  batch_size=64,
                  sample_weights=None,
                  l2_coef=0.0,
                  **kwargs):
    X, pred, y = in_model
    X_test, y_test, X_test_weights, = test_data

    if sample_weights is None:
        sample_weights = np.ones(y_test.shape[0])
    batch_sample_weight = tf.placeholder(tf.float32, [None, 1])

    # Define loss and optimizer
    loss_op = get_loss_function(pred, y, batch_sample_weight, l2_coef=l2_coef)

    batch_gen = batch_generator(X_test, y_test, sample_weights, batch_size)
    batch_losses = []
    for batch_x, batch_y in batch_gen:
        _, train_batch_loss = session.run([loss_op],
                                          feed_dict={X: batch_x, y: batch_y})
        batch_losses.append(train_batch_loss)
    return np.mean(batch_losses)


def load(in_model_folder):
    with open(path.join(in_model_folder, 'rev_vocab')) as rev_vocab_in:
        rev_vocab = json.load(rev_vocab_in)
    model = load_model(path.join(in_model_folder, MODEL_FILENAME))
    global TF_GRAPH
    TF_GRAPH = tf.get_default_graph()
    return model, rev_vocab


def predict(in_model, in_X):
    global TF_GRAPH
    with TF_GRAPH.as_default():
        return in_model.predict(in_X)


def save_vocabulary(in_vocabulary, in_file):
    with open(in_file, 'w') as vocabulary_out:
        json.dump(in_vocabulary, vocabulary_out)


def load_vocabulary(in_file):
    with open(in_file) as vocabulary_in:
        vocab = filter(
            lambda word: len(word),
            [word.strip() for word in vocabulary_in]
        )
    rev_vocab = {word: index for index, word in enumerate(vocab)}
    return vocab, rev_vocab


def make_dataset(in_table, in_rev_vocab, config, use_sample_weights=True):
    # n lists (#context_turns) of lists
    max_context_turns = config['max_context_turns']
    max_sequence_length = config['max_sequence_length']
    questions_tokenized = [[] for _ in xrange(max_context_turns)]
    for context, context_nes in zip(in_table.context, in_table.context_ne):
        context_turns = [[] for _ in xrange(max_context_turns - context)] + context_turns
        for turn_idx, (turn, turn_nes) in enumerate(zip(context_turns, context_nes)):
            questions_tokenized[turn_idx].append(turn + turn_nes)
    answers_tokenized = []
    for answer, answer_nes in zip(in_table.answer, in_table.answer_ne):
        answers_tokenized.append(answer + answer_nes)

    questions_vectorized = []
    for turns_list in questions_tokenized:
        questions_vectorized.append(vectorize_sequences(turns_list, in_rev_vocab))
    answers_vectorized = vectorize_sequences(answers_tokenized, in_rev_vocab)
    questions_padded = [tf.keras.preprocessing.sequence.pad_sequences(questions_list,
                                                                      maxlen=max_sequence_length)
                        for questions_list in questions_vectorized]
    answers_vectorized = tf.keras.preprocessing.sequence.pad_sequences(answers_vectorized,
                                                                       maxlen=max_sequence_length)

    targets = in_table.target

    answer_bot = [bot.partition('-')[0] for bot in in_table.answer_bot]
    context_bots = []
    for bot_list in in_table.context_bots:
        context_bots.append([bot.partition('-')[0] for bot in bot_list])
    bot_overlap_binary = [int(a_bot in q_bots)
                          for a_bot, q_bots in zip(answer_bot, context_bots)]
    q_sentiment = [sentiments[-1] for sentiments in in_table.context_sentiment]

    X = map(np.asarray,
            questions_padded + [answers_vectorized,
                                q_sentiment,
                                [sent for sent in in_table.answer_sentiment],
                                np.expand_dims(in_table.timestamp, -1),
                                np.expand_dims(bot_overlap_binary, -1)])
    if not use_sample_weights:
        return (X,
                np.expand_dims(targets, -1),
                np.expand_dims(np.asarray([1.0 for _ in xrange(len(answers_vectorized))]), -1))
    default_weight = config['bot_sample_weights']['default']
    X_weight = np.asarray([default_weight for _ in xrange(len(in_table['bot']))])
    for index, bot in enumerate(in_table['bot']):
        for bot_prefix, weight in CONFIG['bot_sample_weights'].iteritems():
            X_weight[index] = weight
            break
    return X, np.expand_dims(targets, -1), X_weight


def make_training_data(in_train, in_dev, in_test, in_sample_weight, in_config):
    utterances_tokenized = []
    for context_utterances in in_train.context:
        utterances_tokenized += [tokenize_utterance(utt, add_special_symbols=False, remove_stopwords=False)
                                 for utt in context_utterances]
    utterances_tokenized += map(lambda x: tokenize_utterance(x, add_special_symbols=False, remove_stopwords=False), in_train.answer)

    context_nes = []
    for ne_list in in_train.context_ne:
        context_nes += ne_list
    word_vocab, rev_word_vocab = build_vocabulary(in_train.context.values.tolist() + in_train.answer.values.tolist(),
                                                  max_size=in_config['max_vocab_size'] - in_config['max_ne_vocab_size'])
    ne_vocab, ne_rev_vocab = build_vocabulary(context_nes + in_train.answer_ne.values.tolist(),
                                              max_size=in_config['max_ne_vocab_size'],
                                              add_special_symbols=False)
    unified_vocab = list(set(word_vocab + ne_vocab))
    unified_rev_vocab = {word: index for index, word in enumerate(unified_vocab)}

    in_config['vocab_size'] = len(unified_rev_vocab)
    X, y, X_weight = make_dataset(in_train, unified_rev_vocab, in_config, use_sample_weights=in_sample_weight)
    X_dev, y_dev, X_dev_weight = make_dataset(in_dev,
                                              unified_rev_vocab,
                                              in_config,
                                              use_sample_weights=in_sample_weight)
    X_test, y_test, X_test_weight = make_dataset(in_test,
                                                 unified_rev_vocab,
                                                 in_config,
                                                 use_sample_weights=in_sample_weight)
    return ((X, y, X_weight),
            (X_dev, y_dev, X_dev_weight),
            (X_test, y_test, X_test_weight),
            unified_rev_vocab)


def build_argument_parser():
    result = argparse.ArgumentParser()
    result.add_argument('trainset')
    result.add_argument('devset')
    result.add_argument('testset')
    result.add_argument('model_folder')
    result.add_argument('--bot_sample_weight', action='store_true')
    result.add_argument('--config', default=path.join(path.dirname(__file__), DEFAULT_CONFIG))
    return result


if __name__ == '__main__':
    parser = build_argument_parser()
    args = parser.parse_args()
    trainset = pd.read_json(args.trainset).sample(frac=1).reset_index(drop=True)
    devset = pd.read_json(args.devset).sample(frac=1).reset_index(drop=True)
    testset = pd.read_json(args.testset).sample(frac=1).reset_index(drop=True)

    CONFIG = get_config(args.config)

    train_data, dev_data, test_data, rev_vocab = make_training_data(trainset,
                                                                    devset,
                                                                    testset,
                                                                    args.bot_sample_weight,
                                                                    CONFIG)
    X, y, X_w = train_data
    X_dev, y_dev, X_dev_w = dev_data
    X_test, y_test, X_test_w = test_data

    if not path.exists(args.model_folder):
        makedirs(args.model_folder)

    save_vocabulary(rev_vocab, path.join(args.model_folder, 'rev_vocab'))

    print 'Training with config "{}" :'.format(args.config)
    print json.dumps(CONFIG, indent=2)
    model = create_model(**CONFIG)
    checkpoint_file = path.join(args.model_folder, MODEL_FILENAME)
    with tf.Session() as sess:
        init = tf.global_variables_initializer()
        sess.run(init)
        train(model,
              (X, y, X_w),
              (X_dev, y_dev, X_dev_w),
              (X_test, y_test, X_test_w),
              checkpoint_file,
              sess,
              **CONFIG)
