from collections import namedtuple
import logging
import random

import numpy as np

from .online_sampler import OnlineSampler


logger = logging.getLogger(__name__)


SampleIndices = namedtuple(
    "SampleIndices", ["indices", "weights"])


def _get_indices_and_probabilities(interval_lengths, indices):
    """Given a list of different interval lengths and the indices of interest in
    that list, weight the probability that we will sample one of the indices
    in `indices` based on the interval lengths in that sublist.

    Parameters
    ----------
    interval_lengths : list(int)
    indices : list(int)

    Returns
    -------
    indices, weights : tuple(list, list)
        Tuple of interval indices to sample from and the corresponding
    weights of those intervals.
    """
    select_interval_lens = np.array(interval_lengths)[indices]
    weights = select_interval_lens / float(np.sum(select_interval_lens))

    keep_indices = []
    for index, weight in enumerate(weights):
        if weight > 1e-10:
            keep_indices.append(indices[index])
    if len(keep_indices) == len(indices):
        return indices, weights.tolist()
    else:
        return _get_indices_and_probabilities(
            interval_lengths, keep_indices)


class IntervalsSampler(OnlineSampler):
    """
    Parameters
    ----------
    genome
    query_feature_data
    distinct_features
    intervals_file
    random_seed
    validation_holdout
    test_holdout
    sequence_length
    center_bin_to_predict
    feature_thresholds
    mode

    Attributes
    ----------
    """


    def __init__(self,
                 genome,
                 query_feature_data,
                 distinct_features,
                 intervals_file,
                 sample_negative=False,
                 random_seed=436,
                 validation_holdout=['6', '7'],
                 test_holdout=['8', '9'],
                 sequence_length=1001,
                 center_bin_to_predict=201,
                 feature_thresholds=0.5,
                 mode="train",
                 save_datasets=["test"]):
        super(IntervalsSampler, self).__init__(
            genome,
            query_feature_data,
            distinct_features,
            random_seed=random_seed,
            validation_holdout=validation_holdout,
            test_holdout=test_holdout,
            sequence_length=sequence_length,
            center_bin_to_predict=center_bin_to_predict,
            feature_thresholds=feature_thresholds,
            mode="train",
            save_datasets=["test"])

        self._sample_from_mode = {}
        self._randcache = {}
        for mode in self.modes:
            self._sample_from_mode[mode] = None
            self._randcache[mode] = {"cache": None, "sample_next": 0}

        self.sample_from_intervals = []
        self.interval_lengths = []

        if self._holdout_type == "chromosome":
            self._partition_dataset_chromosome(intervals_file)
        else:
            self._partition_dataset_proportion(intervals_file)

        for mode in self.modes:
            self._update_randcache(mode=mode)

        self.sample_negative = sample_negative

    def _partition_dataset_proportion(self, intervals_file):
        with open(intervals_file, 'r') as file_handle:
            for line in file_handle:
                cols = line.strip().split('\t')
                chrom = cols[0]
                start = int(cols[1])
                end = int(cols[2])
                self.sample_from_intervals.append((chrom, start, end))
                self.interval_lengths.append(end - start)
        n_intervals = len(self.sample_from_intervals)

        # all indices in the intervals list are shuffled
        select_indices = list(range(n_intervals))
        np.random.shuffle(select_indices)

        # the first section of indices is used as the validation set
        n_indices_validate = int(n_intervals * self.validation_holdout)
        val_indices, val_weights = _get_indices_and_probabilities(
            self.interval_lengths, select_indices[:n_indices_validate])
        self._sample_from_mode["validate"] = SampleIndices(
            val_indices, val_weights)

        if self.test_holdout:
            # if applicable, the second section of indices is used as the
            # test set
            n_indices_test = int(n_intervals * self.test_holdout)
            test_indices_end = n_indices_test + n_indices_validate
            test_indices, test_weights = _get_indices_and_probabilities(
                self.interval_lengths,
                select_indices[n_indices_validate:test_indices_end])
            self._sample_from_mode["test"] = SampleIndices(
                test_indices, test_weights)

            # remaining indices are for the training set
            tr_indices, tr_weights = _get_indices_and_probabilities(
                self.interval_lengths, select_indices[test_indices_end:])
            self._sample_from_mode["train"] = SampleIndices(
                tr_indices, tr_weights)
        else:
            # remaining indices are for the training set
            tr_indices, tr_weights = _get_indices_and_probabilities(
                self.interval_lengths, select_indices[n_indices_validate:])
            self._sample_from_mode["train"] = SampleIndices(
                tr_indices, tr_weights)

    def _partition_dataset_chromosome(self, intervals_file):
        for mode in self.modes:
            self._sample_from_mode[mode] = SampleIndices([], [])
        with open(intervals_file, 'r') as file_handle:
            for index, line in enumerate(file_handle):
                cols = line.strip().split('\t')
                chrom = cols[0]
                start = int(cols[1])
                end = int(cols[2])
                if chrom in self.validation_holdout:
                    self._sample_from_mode["validate"].indices.append(
                        index)
                elif self.test_holdout and chrom in self.test_holdout:
                    self._sample_from_mode["test"].indices.append(
                        index)
                else:
                    self._sample_from_mode["train"].indices.append(
                        index)
                self.sample_from_intervals.append((chrom, start, end))
                self.interval_lengths.append(end - start)

        for mode in self.modes:
            sample_indices = self._sample_from_mode[mode].indices
            indices, weights = _get_indices_and_probabilities(
                self.interval_lengths, sample_indices)
            self._sample_from_mode[mode] = \
                self._sample_from_mode[mode]._replace(
                    indices=indices, weights=weights)

    def _retrieve(self, chrom, position):
        bin_start = position - self._start_radius
        bin_end = position + self._end_radius
        retrieved_targets = self.query_feature_data.get_feature_data(
            chrom, bin_start, bin_end)
        if not self.sample_negative and np.sum(retrieved_targets) == 0:
            logger.info("No features found in region surrounding "
                        "chr{0} position {1}. Sampling again.".format(
                            chrom, position))
            return None

        window_start = bin_start - self.surrounding_sequence_radius
        window_end = bin_end + self.surrounding_sequence_radius
        strand = self.STRAND_SIDES[random.randint(0, 1)]
        retrieved_seq = \
            self.genome.get_encoding_from_coords(
                f"chr{chrom}", window_start, window_end, strand)
        if retrieved_seq.shape[0] == 0:
            logger.info("Full sequence centered at chr{0} position {1} "
                        "could not be retrieved. Sampling again.".format(
                            chrom, position))
            return None
        elif np.sum(retrieved_seq) / float(retrieved_seq.shape[0]) < 0.60:
            logger.info("Over 30% of the bases in the sequence centered "
                        "at chr{0} position {1} are ambiguous ('N'). "
                        "Sampling again.".format(chrom, position))
            return None

        if self.mode in self.save_datasets:
            feature_indices = ';'.join(
                [str(f) for f in np.nonzero(retrieved_targets)[0]])
            self.save_datasets[self.mode].append(
                [f"chr{chrom}",
                 window_start,
                 window_end,
                 strand,
                 feature_indices])
        return (retrieved_seq, retrieved_targets)

    def _update_randcache(self, mode=None):
        if not mode:
            mode = self.mode
        self._randcache[mode]["cache_indices"] = np.random.choice(
            self._sample_from_mode[mode].indices,
            size=len(self._sample_from_mode[mode].indices),
            replace=True,
            p=self._sample_from_mode[mode].weights)
        self._randcache[mode]["sample_next"] = 0

    def sample(self, batch_size=1):
        sequences = np.zeros((batch_size, self.sequence_length, 4))
        targets = np.zeros((batch_size, self.n_features))
        n_samples_drawn = 0
        while n_samples_drawn < batch_size:
            sample_index = self._randcache[self.mode]["sample_next"]
            if sample_index == len(self._sample_from_mode[self.mode].indices):
                self._update_randcache()
                sample_index = 0

            rand_interval_index = \
                self._randcache[self.mode]["cache_indices"][sample_index]
            self._randcache[self.mode]["sample_next"] += 1

            interval_info = self.sample_from_intervals[rand_interval_index]
            interval_length = self.interval_lengths[rand_interval_index]

            chrom = interval_info[0]
            position = int(
                interval_info[1] + random.uniform(0, 1) * interval_length)

            retrieve_output = self._retrieve(chrom, position)
            if not retrieve_output:
                continue
            seq, seq_targets = retrieve_output
            sequences[n_samples_drawn, :, :] = seq
            targets[n_samples_drawn, :] = seq_targets
            n_samples_drawn += 1
        return (sequences, targets)

    def get_data_and_targets(self, mode, batch_size, n_samples):
        self.set_mode(mode)
        sequences_and_targets = []
        targets_mat = []

        n_batches = int(n_samples / batch_size)
        for _ in range(n_batches):
            inputs, targets = self.sample(batch_size)
            sequences_and_targets.append((inputs, targets))
            targets_mat.append(targets)
        targets_mat = np.vstack(targets_mat)
        return sequences_and_targets, targets_mat

    def get_dataset_in_batches(self, mode, batch_size, n_samples=None):
        if not n_samples:
            n_samples = len(self._sample_from_mode[mode].indices)
        return self.get_data_and_targets(mode, batch_size, n_samples)

    def get_validation_set(self, batch_size, n_samples=None):
        return self.get_dataset_in_batches(
            "validate", batch_size, n_samples=n_samples)

    def get_test_set(self, batch_size, n_samples=None):
        return self.get_dataset_in_batches("test", batch_size, n_samples)