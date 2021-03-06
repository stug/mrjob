# Copyright 2019 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Test the Spark Harness."""
from io import BytesIO
from os.path import join

from mrjob.examples.mr_word_freq_count import MRWordFreqCount
from mrjob.job import MRJob
from mrjob.local import LocalMRJobRunner
from mrjob.protocol import TextProtocol
from mrjob.spark import mrjob_spark_harness
from mrjob.spark.mr_spark_harness import MRSparkHarness
from mrjob.step import INPUT
from mrjob.step import OUTPUT
from mrjob.util import cmd_line
from mrjob.util import to_lines

from tests.mr_doubler import MRDoubler
from tests.mr_streaming_and_spark import MRStreamingAndSpark
from tests.mr_sort_and_group import MRSortAndGroup
from tests.mr_two_step_job import MRTwoStepJob
from tests.sandbox import SandboxedTestCase
from tests.sandbox import SingleSparkContextTestCase



def _rev(s):
    return ''.join(reversed(s))


class ReversedTextProtocol(TextProtocol):
    """Like TextProtocol, but stores text backwards."""

    def read(self, line):
        key, value = super(ReversedTextProtocol, self).read(line)

        return _rev(key), _rev(value)

    def write(self, key, value):
        return super(ReversedTextProtocol, self).write(
            _rev(key), _rev(value))


class MRSortAndGroupReversedText(MRSortAndGroup):

    INTERNAL_PROTOCOL = ReversedTextProtocol


class MRPassThruArgTest(MRJob):

    def configure_args(self):
        super(MRPassThruArgTest, self).configure_args()
        self.add_passthru_arg('--chars', action='store_true')
        self.add_passthru_arg('--ignore')

    def mapper(self, _, value):
        if self.options.ignore:
            value = value.replace(self.options.ignore, '')
        if self.options.chars:
            for c in value:
                yield c, 1
        else:
            for w in value.split(' '):
                yield w, 1

    def reducer(self, key, values):
        yield key, sum(values)


class SparkHarnessOutputComparisonTestCase(
        SandboxedTestCase, SingleSparkContextTestCase):

    def _spark_harness_path(self):
        path = mrjob_spark_harness.__file__
        if path.endswith('.pyc'):
            path = path[:-1]

        return path

    def _reference_job(self, job_class, input_bytes=b'', input_paths=(),
                       runner_alias='inline', job_args=[]):

        args = ['-r', runner_alias] + list(job_args) + list(input_paths)

        reference_job = job_class(args)
        reference_job.sandbox(stdin=BytesIO(input_bytes))

        return reference_job

    def _harness_job(self, job_class, input_bytes=b'', input_paths=(),
                     runner_alias='inline', compression_codec=None,
                     job_args=None, start_step=None, end_step=None):
        job_class_path = '%s.%s' % (job_class.__module__, job_class.__name__)

        harness_job_args = ['-r', runner_alias, '--job-class', job_class_path]
        if compression_codec:
            harness_job_args.append('--compression-codec')
            harness_job_args.append(compression_codec)
        if job_args:
            harness_job_args.extend(['--job-args', cmd_line(job_args)])
        if start_step:
            harness_job_args.extend(['--start-step', str(start_step)])
        if end_step:
            harness_job_args.extend(['--end-step', str(end_step)])

        harness_job_args.extend(input_paths)

        harness_job = MRSparkHarness(harness_job_args)
        harness_job.sandbox(stdin=BytesIO(input_bytes))

        return harness_job

    def _assert_output_matches(
            self, job_class, input_bytes=b'', input_paths=(), job_args=[]):

        reference_job = self._reference_job(
            job_class, input_bytes=input_bytes,
            input_paths=input_paths,
            job_args=job_args)

        with reference_job.make_runner() as runner:
            runner.run()

            reference_output = sorted(to_lines(runner.cat_output()))

        harness_job = self._harness_job(
            job_class, input_bytes=input_bytes,
            input_paths=input_paths,
            job_args=job_args)

        with harness_job.make_runner() as runner:
            runner.run()

            harness_output = sorted(to_lines(runner.cat_output()))

        self.assertEqual(harness_output, reference_output)

    def test_basic_job(self):
        input_bytes = b'one fish\ntwo fish\nred fish\nblue fish\n'

        self._assert_output_matches(MRWordFreqCount, input_bytes=input_bytes)

    def test_two_step_job(self):
        input_bytes = b'foo\nbar\n'

        self._assert_output_matches(MRTwoStepJob, input_bytes=input_bytes)

    def test_mixed_job(self):
        # can we run just the streaming part of a job?
        input_bytes = b'foo\nbar\n'

        job = self._harness_job(
            MRStreamingAndSpark, input_bytes=input_bytes,
            start_step=0, end_step=1)

        with job.make_runner() as runner:
            runner.run()

            # the streaming part is just an identity mapper, but it converts
            # lines to pairs of JSON
            self.assertEqual(set(to_lines(runner.cat_output())),
                             {b'null\t"foo"\n', b'null\t"bar"\n'})

    def test_range_of_steps(self):
        # check for off-by-one errors, etc.
        input_bytes = b'"three"\t3\n"five"\t5'

        # sanity-check
        self._assert_output_matches(MRDoubler, input_bytes=input_bytes,
                                    job_args=['-n', '5'])

        # just run two of the five steps
        steps_2_and_3_job = self._harness_job(
            MRDoubler, input_bytes=input_bytes, job_args=['-n', '5'],
            start_step=2, end_step=4)

        with steps_2_and_3_job.make_runner() as runner:
            runner.run()

            # parse_output() works because internal and output protocols match
            self.assertEqual(
                dict(steps_2_and_3_job.parse_output(runner.cat_output())),
                dict(three=12, five=20),
            )

    def test_compression(self):
        compression_codec = 'org.apache.hadoop.io.compress.GzipCodec'

        input_bytes = b'fa la la la la\nla la la la\n'

        job = self._harness_job(
            MRWordFreqCount, input_bytes=input_bytes,
            compression_codec=compression_codec)

        with job.make_runner() as runner:
            runner.run()

            self.assertTrue(runner.fs.exists(
                join(runner.get_output_dir(), 'part*.gz')))

            self.assertEqual(dict(job.parse_output(runner.cat_output())),
                             dict(fa=1, la=8))

    def test_sort_values(self):
        input_bytes = (
            b'alligator\nactuary\nbowling\nartichoke\nballoon\nbaby\n')

        self._assert_output_matches(MRSortAndGroup, input_bytes=input_bytes)

    def test_sort_values_sorts_encoded_values(self):
        input_bytes = (
            b'alligator\nactuary\nbowling\nartichoke\nballoon\nbaby\n')

        job = self._harness_job(MRSortAndGroupReversedText,
                                input_bytes=input_bytes)

        with job.make_runner() as runner:
            runner.run()

            self.assertEqual(
                dict(job.parse_output(runner.cat_output())),
                dict(a=['artichoke', 'alligator', 'actuary'],
                     b=['bowling', 'balloon', 'baby']))

    def test_passthru_args(self):
        input_bytes = b'\n'.join([
            b'to be or',
            b'not to be',
            b'that is the question'])

        self._assert_output_matches(
            MRPassThruArgTest,
            input_bytes=input_bytes,
            job_args=['--chars', '--ignore', 'to'])


# TODO: add back a test of the bare Harness script in local mode (slow)
