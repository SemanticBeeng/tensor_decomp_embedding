import itertools
import numpy as np
import os
import tensorflow as tf
from tensor_decomp import CPDecomp
import time


class TensorEmbedding(object):
    def __init__(self, vocab_model, embedding_dim, window_size=10, optimizer_type='adam', ndims=2):
        self.model = vocab_model
        self.embedding_dim = embedding_dim
        self.window_size = window_size  # 10 (|left context| + |right context|)
        self.optimizer_type = optimizer_type
        self.ndims = ndims
        if self.ndims > 3:
            raise ValueError('As of right now, ndims can be at most 3')

        self.vocab_len = len(self.model.vocab)
        # t-th batch tensor
        # contains all data for this minibatch. already summed/averaged/whatever it needs to be. 
        config = tf.ConfigProto(
            allow_soft_placement=True,
        )
        self.sess = tf.Session(config=config)
        with self.sess.as_default():
            self.X_t = tf.sparse_placeholder(tf.float32, shape=np.array([self.vocab_len] * self.ndims, dtype=np.int64))
            # Goal: X_ijk == sum_{r=1}^{R} U_{ir} V_{jr} W_{kr}
            self.U = tf.Variable(tf.random_uniform(
                shape=[self.vocab_len, self.embedding_dim],
                minval=-1.0,
                maxval=1.0,
            ), name="U")
            self.V = tf.Variable(tf.random_uniform(
                shape=[self.vocab_len, self.embedding_dim],
                minval=-1.0,
                maxval=1.0,
            ), name="V")
            if self.ndims > 2:
                self.W = tf.Variable(tf.random_uniform(
                    shape=[self.vocab_len, self.embedding_dim],
                    minval=-1.0,
                    maxval=1.0,
                ), name="W")
            self.create_loss_fn(reg_param=1e-8)

    def update_counts_with_sent_info(self, sent, counts):
        """
        `sent` is a tuple of tensors representing the word and the context.
            For example, ([74895, 1397, 2385, 23048, 9485, 58934, 2378, 51143, 35829, 34290], 15234)
        """
        context_list, word = sent
        if self.ndims == 2:
            for context_word in context_indices:
                context_index = (word, context_word)
                if context_index not in counts:
                    counts[context_index] = 1
                else:
                    counts[context_index] += 1
            return counts
        elif self.ndims == 3:
            context_indices = itertools.product(context_list, context_list)  # e.g., [(74895, 1397), (74895, 2385), ...]
            for context_word1, context_word2 in context_indices:
                context_index = (word, context_word1, context_word2)
                if context_index not in counts:
                    counts[context_index] = 1
                else:
                    counts[context_index] += 1
            return counts

    def train_on_batch(self, batch):
        """
        `batch` is a list of tuples of tensors representing the word and the context.
            For example, [
                ([74895, 1397, 2385, 23048, 9485, 58934, 2378, 51143, 35829, 34290], 15234), 
                ...,
            ]
        """
        ## Create input tensor
        counts = {}
        for sent in batch:
            self.update_counts_with_sent_info(sent, counts)

        # https://www.tensorflow.org/api_docs/python/io_ops/placeholders#sparse_placeholder
        counts_iter = counts.items()
        if self.ndims == 2:
            sent_tensor = tf.SparseTensorValue(
                indices=[pair for pair, _ in counts_iter], # e.g., [(15234, 74895), (15234, 2385), ...] 
                values=[count for _, count in counts_iter],
                shape=[self.vocab_len, self.vocab_len],
            )
        elif self.ndims == 3:
            sent_tensor = tf.SparseTensorValue(
                indices=[triple for triple, _ in counts_iter], # e.g., [(15234, 74895, 1397), (15234, 74895, 2385), ...] 
                values=[count for _, count in counts_iter],
                shape=[self.vocab_len, self.vocab_len, self.vocab_len],
            )
            # this tensor takes about .12 seconds to make. Too slow? Since we're doing hundreds of thousands of batches

        # Update the online version of the CP decomp, given a batch of words and contexts
        ## Feed input tensor to minimization algorithm
        self.train_step(sent_tensor)

    def train_step(self, sent_tensor, print_every=10, evaluate_every=5000):
        if not hasattr(self, 'prev_time'):
            self.prev_time = time.time()
        feed_dict = {
            self.X_t: sent_tensor,
        }
        _, loss, step = self.sess.run(
            [
                self.train_op,
                self.loss,
                self.global_step,
            ],
            feed_dict=feed_dict,
        )

        if step % print_every == 0:
            print("Loss at step {}: {} (avg time per batch: {})".format(step, loss, (time.time() - self.prev_time) / print_every))
            self.prev_time = time.time()
        
    def create_loss_fn(self, reg_param):
        """
        L(X; U,V,W) = .5 sum_{i,j,k where X_ijk =/= 0} (X_ijk - sum_{r=1}^{R} U_ir V_jr W_kr)^2
        L_{rho} = L(X; U,V,W) + rho * (||U||^2 + ||V||^2 + ||W||^2) where ||.|| represents some norm (L2, L1, Frobenius)
        """
        def L(X, U,V,W):
            """
            X is a sparse tensor. U,V,W are dense. 
            """
            X_ijks = X.values  # of shape (N,) - represents all the values stored in X. 
            indices = tf.transpose(X.indices)  # of shape (N,3) - represents the indices of all values (in the same order as X.values)

            U_indices = tf.gather(indices, 0)  # of shape (N,) - represents all the indices to get from the U matrix
            V_indices = tf.gather(indices, 1)
            W_indices = tf.gather(indices, 2) # there better be an error!
            U_vects = tf.gather(U, U_indices)  # of shape (N, R) - each index represents the 1xR vector found in U_i
            V_vects = tf.gather(V, V_indices)
            W_vects = tf.gather(W, W_indices)
            # TODO: MAKE SURE U_vects correspond to the values in X.values!!!!!!!!!!

            # elementwise multiplication of each of U, V, and W - the first step in getting <U_i, V_j, W_k>, as a triple dot product (for each i,j,k in X)
            # we are calculating the matrix UVW (of shape N,R), where UVW_(m,:) = U_ir * V_jr * W_kr, where X.indices[m] = i,j,k.
            elementwise_product = tf.multiply(tf.multiply(U_vects, V_vects), W_vects)  # of shape (N, R)
                                                                                
            predicted_X_ijks = tf.reduce_sum(elementwise_product, axis=1)  # of shape (N,) - represents Sum_{r=1}^R U_ir V_jr W_kr
            errors = tf.square(X_ijks - predicted_X_ijks)  # of shape (N,) - elementwise error for each entry in X_ijk

            mean_loss = .5 * tf.reduce_sum(errors)  # average loss per entry in X - scalar!

            return mean_loss

        def reg(U,V,W):
            # NOTE: l2_loss already squares the norms. So we don't need to square them.
            summed_norms = (
                tf.nn.l2_loss(U, name="U_norm") +
                tf.nn.l2_loss(V, name="V_norm") +
                tf.nn.l2_loss(W, name="W_norm")
            )
            return (.5 * reg_param) * summed_norms

        self.loss = L(self.X_t, self.U,self.V,self.W) + reg(self.U, self.V, self.W)
        
    def get_train_op(self):
        # TODO: implement SALS or 2SGD. Also experiment with just using ADAM/SGD (builtin) to minimize the loss
        if self.optimizer_type == '2sgd':
            return self.get_train_op_2sgd()
        elif self.optimizer_type == 'sals':
            return self.get_train_op_sals()
        elif self.optimizer_type == 'adam':
            return self.get_train_op_adam()
        elif self.optimizer_type == 'sgd':
            return self.get_train_op_sgd()

    def get_train_op_2sgd(self, rho=1e-4):
        '''
        See 2SGD algorithm in Expected Tensor Decomp paper
        '''
        X = self.X_t
        U = self.U
        V = self.V
        W = self.W
        t = self.global_step
        eta_t = 1. / (1. + t)

        # X(.,V,W)_ir = sum_{j,k} X_ijk * V_jr * W_kr
        import pdb; pdb.set_trace()
        modified_X = tf.einsum('ijk,jr,kr->ir', X, V, W)

        def contract_X(X, V, W):
            result = tf.Variable(tf.zeros(shape=[self.vocab_len, self.embedding_dim]), name='XVW')
            # TODO: generalize to X(U.W) and X(UV.)
            values = X.values
            indices = tf.transpose(X.indices)

            i_s = tf.gather(indices, 0)
            j_s = tf.gather(indices, 1)
            k_s = tf.gather(indices, 2)
            # TODO: do it out :)
            # for each (value, index) pair, add to result_ir
            for value, index in values, indices:
                result[index.i, :] += value * tf.multiply(v[index.j], w[index.k])

        def gamma(A,B):
            ATA = tf.matmul(A,A, transpose_a=True)  # A^T * A
            BTB = tf.matmul(B,B, transpose_a=True)  # B^T * B
            return tf.multiply(ATA, BTB)  # hadamard product of A^T*A and B^T*B

        gamma_rho = gamma(V,W) + rho * tf.eye(self.embedding_dim)
        inv_gamma_rho = tf.matrix_inverse(gamma_rho)
        grad_value = tf.matmul(modified_X, inv_gamma_rho)
        tf.assign(U, (1-eta_t) * U + eta_t * grad_value)

    def get_train_op_sals(self):
        pass

    def get_train_op_adam(self):
        return self.optimizer.minimize(self.loss, global_step=self.global_step)

    def get_train_op_sgd(self):
        return self.optimizer.minimize(self.loss, global_step=self.global_step)

    def write_embedding_to_file(self, fname='vectors.txt'):
        vectors = {}
        model = self.model
        embedding = self.get_embedding_matrix()
        count = 0 # number of vects written
        for word in model.vocab:
            word_vocab = model.vocab[word]
            word_vect = embedding[word_vocab.index]
            vect_list = ['{:.3f}'.format(x) for x in word_vect]
            vectors[word] = ' '.join(vect_list)
        with open(fname, 'w') as f:
            for word in vectors:
                if not word:
                    continue
                try:
                    f.write(word.encode('utf-8') + ' ' + vectors[word] + '\n')
                    count += 1
                except TypeError:
                    f.write(word + ' ' + vectors[word] + '\n')
                    count += 1
                except:
                    pass
        with open(fname, 'r+') as f:
            content = f.read()
            f.seek(0, 0)
            f.write('{} {}\n'.format(count, self.embedding_dim))  # write the number of vects
            f.write(content)

    def evaluate(self, rel_path='vectors.txt'):
        self.write_embedding_to_file(fname=rel_path)
        method = None
        method = self.optimizer_type
        out_fname = 'results_iter{}_{}.txt'.format(self.batch_num, method)
        os.system('time python3 embedding_benchmarks/scripts/evaluate_on_all.py -f /home/eric/code/gensim/{} -o /home/eric/code/gensim/results/{}'.format(rel_path, out_fname))
        print('done evaluating.')

    def get_embedding_matrix(self):
        embedding = self.U.eval(self.sess)
        return embedding

    def train(self, batches):
        self.batch_num = 0
        with tf.device('/gpu:0'):
            self.global_step = tf.Variable(0.0, name='global_step', trainable=False)
            if self.optimizer_type == 'adam':
                self.optimizer = tf.train.AdamOptimizer(learning_rate=1e-3)
            elif self.optimizer_type == 'sgd':
                self.optimizer = tf.train.GradientDescentOptimizer(learning_rate=1e-2)

            self.train_op = self.get_train_op()

            self.sess.run(tf.initialize_all_variables())
            with self.sess.as_default():
                for batch in batches:
                    if self.batch_num % 500 == 0:
                        self.evaluate()
                    self.train_on_batch(batch)
                    self.batch_num += 1
