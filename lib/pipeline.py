import sys
from abc import ABCMeta, abstractmethod
import os
import csv
import datetime
import time

try:
    from .. import lib
except ValueError:
    import lib
from lib.prediction_class import *
from lib.input_file_converter import *
from lib.fasta_generator import *
from lib.output_parser import *
from lib.post_processor import *
import shutil
import yaml
import pkg_resources
import pymp
from threading import Lock

def status_message(msg):
    print(msg)
    sys.stdout.flush()

def status_message_with_lock(msg, lock):
    lock.acquire()
    status_message(msg)
    lock.release()

class Pipeline(metaclass=ABCMeta):
    def __init__(self, **kwargs):
        self.input_file                  = kwargs['input_file']
        self.input_file_type             = kwargs['input_file_type']
        self.sample_name                 = kwargs['sample_name']
        self.alleles                     = kwargs['alleles']
        self.prediction_algorithms       = kwargs['prediction_algorithms']
        self.output_dir                  = kwargs['output_dir']
        self.peptide_sequence_length     = kwargs.pop('peptide_sequence_length', 21)
        self.epitope_lengths             = kwargs['epitope_lengths']
        self.iedb_executable             = kwargs.pop('iedb_executable', None)
        self.gene_expn_file              = kwargs.pop('gene_expn_file', None)
        self.transcript_expn_file        = kwargs.pop('transcript_expn_file', None)
        self.normal_snvs_coverage_file   = kwargs.pop('normal_snvs_coverage_file', None)
        self.normal_indels_coverage_file = kwargs.pop('normal_indels_coverage_file', None)
        self.tdna_snvs_coverage_file     = kwargs.pop('tdna_snvs_coverage_file', None)
        self.tdna_indels_coverage_file   = kwargs.pop('tdna_indels_coverage_file', None)
        self.trna_snvs_coverage_file     = kwargs.pop('trna_snvs_coverage_file', None)
        self.trna_indels_coverage_file   = kwargs.pop('trna_indels_coverage_file', None)
        self.phased_proximal_variants_vcf = kwargs.pop('phased_proximal_variants_vcf', None)
        self.net_chop_method             = kwargs.pop('net_chop_method', None)
        self.net_chop_threshold          = kwargs.pop('net_chop_threshold', 0.5)
        self.netmhc_stab                 = kwargs.pop('netmhc_stab', False)
        self.top_score_metric            = kwargs.pop('top_score_metric', 'median')
        self.binding_threshold           = kwargs.pop('binding_threshold', 500)
        self.allele_specific_binding_thresholds = kwargs.pop('allele_specific_cutoffs', False)
        self.minimum_fold_change         = kwargs.pop('minimum_fold_change', 0)
        self.normal_cov                  = kwargs.pop('normal_cov', None)
        self.normal_vaf                  = kwargs.pop('normal_vaf', None)
        self.tdna_cov                    = kwargs.pop('tdna_cov', None)
        self.tdna_vaf                    = kwargs.pop('tdna_vaf', None)
        self.trna_cov                    = kwargs.pop('trna_cov', None)
        self.trna_vaf                    = kwargs.pop('trna_vaf', None)
        self.expn_val                    = kwargs.pop('expn_val', None)
        self.maximum_transcript_support_level = kwargs.pop('maximum_transcript_support_level', None)
        self.additional_report_columns   = kwargs.pop('additional_report_columns', None)
        self.fasta_size                  = kwargs.pop('fasta_size', 200)
        self.iedb_retries                = kwargs.pop('iedb_retries', 5)
        self.downstream_sequence_length  = kwargs.pop('downstream_sequence_length', 1000)
        self.keep_tmp_files              = kwargs.pop('keep_tmp_files', False)
        self.exclude_NAs                 = kwargs.pop('exclude_NAs', False)
        self.pass_only                   = kwargs.pop('pass_only', False)
        self.normal_sample_name          = kwargs.pop('normal_sample_name', False)
        self.n_threads                   = kwargs.pop('n_threads', 1)
        self.spacers                     = kwargs.pop('spacers', None)
        self.proximal_variants_file      = None
        tmp_dir = os.path.join(self.output_dir, 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        self.tmp_dir = tmp_dir

    def log_dir(self):
        dir = os.path.join(self.output_dir, 'log')
        os.makedirs(dir, exist_ok=True)
        return dir

    def print_log(self):
        log_file = os.path.join(self.log_dir(), 'inputs.yml')
        if os.path.exists(log_file):
            with open(log_file, 'r') as log_fh:
                past_inputs = yaml.load(log_fh)
                current_inputs = self.__dict__
                current_inputs['pvactools_version'] = pkg_resources.get_distribution("pvactools").version
                if past_inputs['pvactools_version'] != current_inputs['pvactools_version']:
                    status_message(
                        "Restart to be executed with a different pVACtools version:\n" +
                        "Past version: %s\n" % past_inputs['pvactools_version'] +
                        "Current version: %s" % current_inputs['pvactools_version']
                    )
                for key in current_inputs.keys():
                    if key == 'pvactools_version' or key == 'pvacseq_version':
                        continue
                    if key not in past_inputs.keys() and current_inputs[key] is not None:
                        sys.exit(
                            "Restart inputs are different from past inputs: \n" +
                            "Additional input: %s - %s\n" % (key, current_inputs[key]) +
                            "Aborting."
                        )
                    elif current_inputs[key] != past_inputs[key]:
                        sys.exit(
                            "Restart inputs are different from past inputs: \n" +
                            "Past input: %s - %s\n" % (key, past_inputs[key]) +
                            "Current input: %s - %s\n" % (key, current_inputs[key]) +
                            "Aborting."
                        )
        else:
            with open(log_file, 'w') as log_fh:
                inputs = self.__dict__
                inputs['pvactools_version'] = pkg_resources.get_distribution("pvactools").version
                yaml.dump(inputs, log_fh, default_flow_style=False)

    def tsv_file_path(self):
        if self.input_file_type == 'pvacvector_input_fasta':
            return self.input_file
        else:
            tsv_file = self.sample_name + '.tsv'
            return os.path.join(self.output_dir, tsv_file)

    def converter(self, params):
        converter_types = {
            'vcf'  : 'VcfConverter',
            'bedpe': 'IntegrateConverter',
        }
        converter_type = converter_types[self.input_file_type]
        converter = getattr(sys.modules[__name__], converter_type)
        return converter(**params)

    def fasta_generator(self, params):
        generator_types = {
            'vcf'                   : 'FastaGenerator',
            'bedpe'                 : 'FusionFastaGenerator',
            'pvacvector_input_fasta': 'VectorFastaGenerator',
        }
        generator_type = generator_types[self.input_file_type]
        generator = getattr(sys.modules[__name__], generator_type)
        return generator(**params)

    def output_parser(self, params):
        parser_types = {
            'vcf'  : 'DefaultOutputParser',
            'bedpe': 'FusionOutputParser',
            'pvacvector_input_fasta': 'VectorOutputParser',
        }
        parser_type = parser_types[self.input_file_type]
        parser = getattr(sys.modules[__name__], parser_type)
        return parser(**params)

    def convert_vcf(self):
        status_message("Converting .%s to TSV" % self.input_file_type)
        if os.path.exists(self.tsv_file_path()):
            status_message("TSV file already exists. Skipping.")
            return

        convert_params = {
            'input_file' : self.input_file,
            'output_file': self.tsv_file_path(),
            'sample_name': self.sample_name,
            'pass_only': self.pass_only,
        }
        for attribute in [
            'gene_expn_file',
            'transcript_expn_file',
            'normal_snvs_coverage_file',
            'normal_indels_coverage_file',
            'tdna_snvs_coverage_file',
            'tdna_indels_coverage_file',
            'trna_snvs_coverage_file',
            'trna_indels_coverage_file',
            'normal_sample_name',
        ]:
            if getattr(self, attribute):
                convert_params[attribute] = getattr(self, attribute)
            else:
                convert_params[attribute] = None
        if self.phased_proximal_variants_vcf is not None:
            convert_params['proximal_variants_vcf'] = self.phased_proximal_variants_vcf
            proximal_variants_tsv = os.path.join(self.output_dir, self.sample_name + '.proximal_variants.tsv')
            convert_params['proximal_variants_tsv'] = proximal_variants_tsv
            self.proximal_variants_file = proximal_variants_tsv
            convert_params['peptide_length'] = self.peptide_sequence_length

        converter = self.converter(convert_params)
        converter.execute()
        print("Completed")

    def tsv_entry_count(self):
        with open(self.tsv_file_path()) as tsv_file:
            reader  = csv.DictReader(tsv_file, delimiter='\t')
            row_count = 0
            for row in reader:
                row_count += 1
        return row_count

    def split_tsv_file(self, total_row_count):
        status_message("Splitting TSV into smaller chunks")
        tsv_size = self.fasta_size / 2
        chunks = []
        with open(self.tsv_file_path(), 'r') as tsv_file:
            reader      = csv.DictReader(tsv_file, delimiter='\t')
            row_count   = 1
            split_start = row_count
            split_end   = split_start + tsv_size - 1
            if split_end > total_row_count:
                split_end = total_row_count
            status_message("Splitting TSV into smaller chunks - Entries %d-%d" % (split_start, split_end))
            split_tsv_file_path = "%s_%d-%d" % (self.tsv_file_path(), split_start, split_end)
            chunks.append([split_start, split_end])
            if os.path.exists(split_tsv_file_path):
                status_message("Split TSV file for Entries %d-%d already exists. Skipping." % (split_start, split_end))
                skip = 1
            else:
                split_tsv_file      = open(split_tsv_file_path, 'w')
                split_tsv_writer    = csv.DictWriter(split_tsv_file, delimiter='\t', fieldnames = reader.fieldnames)
                split_tsv_writer.writeheader()
                skip = 0
            for row in reader:
                if skip == 0:
                    split_tsv_writer.writerow(row)
                if row_count == total_row_count:
                    break
                if row_count % tsv_size == 0:
                    if skip == 0:
                        split_tsv_file.close()
                    split_start = row_count + 1
                    split_end   = split_start + tsv_size - 1
                    if split_end > total_row_count:
                        split_end = total_row_count
                    status_message("Splitting TSV into smaller chunks - Entries %d-%d" % (split_start, split_end))
                    split_tsv_file_path = "%s_%d-%d" % (self.tsv_file_path(), split_start, split_end)
                    chunks.append([split_start, split_end])
                    if os.path.exists(split_tsv_file_path):
                        status_message("Split TSV file for Entries %d-%d already exists. Skipping." % (split_start, split_end))
                        skip = 1
                    else:
                        split_tsv_file      = open(split_tsv_file_path, 'w')
                        split_tsv_writer    = csv.DictWriter(split_tsv_file, delimiter='\t', fieldnames = reader.fieldnames)
                        split_tsv_writer.writeheader()
                        skip = 0
                row_count += 1
            if skip == 0:
                split_tsv_file.close()
        status_message("Completed")
        return chunks

    def generate_fasta(self, chunks):
        status_message("Generating Variant Peptide FASTA and Key Files")
        for (split_start, split_end) in chunks:
            tsv_chunk = "%d-%d" % (split_start, split_end)
            fasta_chunk = "%d-%d" % (split_start*2-1, split_end*2)
            generate_fasta_params = {
                'peptide_sequence_length'   : self.peptide_sequence_length,
                'downstream_sequence_length': self.downstream_sequence_length,
                'proximal_variants_file'    : self.proximal_variants_file,
            }
            split_fasta_file_path = "%s_%s" % (self.split_fasta_basename(), fasta_chunk)
            if os.path.exists(split_fasta_file_path):
                status_message("Split FASTA file for Entries %s already exists. Skipping." % (fasta_chunk))
                continue
            if self.input_file_type == 'pvacvector_input_fasta':
                generate_fasta_params['input_file'] = self.tsv_file_path()
                generate_fasta_params['output_file_prefix'] = split_fasta_file_path
                generate_fasta_params['epitope_lengths'] = self.epitope_lengths
                generate_fasta_params['spacers'] = self.spacers
            else:
                split_fasta_key_file_path = split_fasta_file_path + '.key'
                generate_fasta_params['input_file'] = "%s_%s" % (self.tsv_file_path(), tsv_chunk)
                generate_fasta_params['epitope_length'] = max(self.epitope_lengths)
                generate_fasta_params['output_file'] = split_fasta_file_path
                generate_fasta_params['output_key_file'] = split_fasta_key_file_path
            status_message("Generating Variant Peptide FASTA and Key Files - Entries %s" % (fasta_chunk))
            fasta_generator = self.fasta_generator(generate_fasta_params)
            fasta_generator.execute()
        status_message("Completed")

    def split_fasta_basename(self):
        return os.path.join(self.tmp_dir, self.sample_name + "_" + str(self.peptide_sequence_length) + ".fa.split")

    def balance_multithreads(self, iteration_info):
        for i in range(1,self.n_threads):
            #find the dimension with the most iterations per thread
            (max_iterations, dimension) = max(zip(map(lambda x: x['iterations_per_thread'], iteration_info.values()), iteration_info.keys()))
            #assign that dimension one more thread
            iteration_info[dimension]['threads'] += 1
            #calculate how many total threads this combination would use since these are nested threads
            total_n_threads = iteration_info['file']['threads'] * iteration_info['allele']['threads'] * iteration_info['length']['threads'] * iteration_info['algorithm']['threads']
            #if total_n_threads exceeds the total number of threads requested, reset to previous state and return
            if total_n_threads > self.n_threads:
                iteration_info[dimension]['threads'] -= 1
                return iteration_info
            #recalculate how many iterations per thread that dimension will have with the new number of threads
            iteration_info[dimension]['iterations_per_thread'] = iteration_info[dimension]['total_iterations'] / iteration_info[dimension]['threads']
            #if all dimension have less or equal to 1 iteration per thread then we can't optimize any further
            if ( iteration_info['file']['iterations_per_thread'] <= 1 and
                 iteration_info['allele']['iterations_per_thread'] <= 1 and
                 iteration_info['length']['iterations_per_thread'] <= 1 and
                 iteration_info['algorithm']['iterations_per_thread'] <= 1 ):
                return iteration_info
        return iteration_info

    def call_iedb_and_parse_outputs(self, chunks):
        pymp.config.nested = True
        alleles = self.alleles
        epitope_lengths = self.epitope_lengths
        prediction_algorithms = self.prediction_algorithms
        iteration_info = {
            'file': {
                'total_iterations': len(chunks),
                'iterations_per_thread': len(chunks),
                'threads': 1,
            },
            'allele': {
                'total_iterations': len(alleles),
                'iterations_per_thread': len(alleles),
                'threads': 1,
            },
            'length': {
                'total_iterations': len(epitope_lengths),
                'iterations_per_thread': len(epitope_lengths),
                'threads': 1,
            },
            'algorithm': {
                'total_iterations': len(prediction_algorithms),
                'iterations_per_thread': len(prediction_algorithms),
                'threads': 1,
            },
        }
        iteration_info = self.balance_multithreads(iteration_info)

        split_parsed_output_files = []
        lock = Lock()
        with pymp.Parallel(iteration_info['file']['threads']) as p:
            for i in p.range(len(chunks)):
                (split_start, split_end) = chunks[i]
                tsv_chunk = "%d-%d" % (split_start, split_end)
                fasta_chunk = "%d-%d" % (split_start*2-1, split_end*2)
                with pymp.Parallel(iteration_info['allele']['threads']) as p2:
                    for j in p2.range(len(alleles)):
                        a = alleles[j]
                        with pymp.Parallel(iteration_info['length']['threads']) as p3:
                            for k in p3.range(len(epitope_lengths)):
                                epl = epitope_lengths[k]
                                if self.input_file_type == 'pvacvector_input_fasta':
                                    split_fasta_file_path = "{}_1-2.{}.tsv".format(self.split_fasta_basename(), epl)
                                else:
                                    split_fasta_file_path = "%s_%s"%(self.split_fasta_basename(), fasta_chunk)
                                split_iedb_output_files = []
                                status_message_with_lock("Processing entries for Allele %s and Epitope Length %s - Entries %s" % (a, epl, fasta_chunk), lock)
                                if os.path.getsize(split_fasta_file_path) == 0:
                                    status_message_with_lock("Fasta file is empty. Skipping", lock)
                                    continue
                                #begin of per-algorithm processing
                                with pymp.Parallel(iteration_info['algorithm']['threads']) as p4:
                                    for m in p4.range(len(prediction_algorithms)):
                                        method = prediction_algorithms[m]
                                        prediction_class = globals()[method]
                                        prediction = prediction_class()
                                        if hasattr(prediction, 'iedb_prediction_method'):
                                            iedb_method = prediction.iedb_prediction_method
                                        else:
                                            iedb_method = method
                                        valid_alleles = prediction.valid_allele_names()
                                        if a not in valid_alleles:
                                            status_message_with_lock("Allele %s not valid for Method %s. Skipping." % (a, method), lock)
                                            continue
                                        valid_lengths = prediction.valid_lengths_for_allele(a)
                                        if epl not in valid_lengths:
                                            status_message_with_lock("Epitope Length %s is not valid for Method %s and Allele %s. Skipping." % (epl, method, a), lock)
                                            continue

                                        split_iedb_out = os.path.join(self.tmp_dir, ".".join([self.sample_name, iedb_method, a, str(epl), "tsv_%s" % fasta_chunk]))
                                        if os.path.exists(split_iedb_out):
                                            status_message_with_lock("IEDB file for Allele %s and Epitope Length %s with Method %s (Entries %s) already exists. Skipping." % (a, epl, method, fasta_chunk), lock)
                                            split_iedb_output_files.append(split_iedb_out)
                                            continue
                                        status_message_with_lock("Running IEDB on Allele %s and Epitope Length %s with Method %s - Entries %s" % (a, epl, method, fasta_chunk), lock)

                                        if not os.environ.get('TEST_FLAG') or os.environ.get('TEST_FLAG') == '0':
                                            if 'last_execute_timestamp' in locals() and not self.iedb_executable:
                                                elapsed_time = ( datetime.datetime.now() - last_execute_timestamp ).total_seconds()
                                                wait_time = 60 - elapsed_time
                                                if wait_time > 0:
                                                    time.sleep(wait_time)

                                        arguments = [
                                            split_fasta_file_path,
                                            split_iedb_out,
                                            method,
                                            a,
                                            '-r', str(self.iedb_retries),
                                            '-e', self.iedb_executable,
                                        ]
                                        if not isinstance(prediction, IEDBMHCII):
                                            arguments.extend(['-l', str(epl),])
                                        lib.call_iedb.main(arguments)
                                        last_execute_timestamp = datetime.datetime.now()
                                        status_message_with_lock("Running IEDB on Allele %s and Epitope Length %s with Method %s - Entries %s - Completed" % (a, epl, method, fasta_chunk), lock)
                                        split_iedb_output_files.append(split_iedb_out)
                                #end of per-algorithm processing

                                #parse all output files for one allele, epitope, and file chunk over all algorithms into one file
                                split_parsed_file_path = os.path.join(self.tmp_dir, ".".join([self.sample_name, a, str(epl), "parsed", "tsv_%s" % fasta_chunk]))
                                if os.path.exists(split_parsed_file_path):
                                    status_message_with_lock("Parsed Output File for Allele %s and Epitope Length %s (Entries %s) already exists. Skipping" % (a, epl, fasta_chunk), lock)
                                    split_parsed_output_files.append(split_parsed_file_path)
                                    continue
                                split_fasta_key_file_path = split_fasta_file_path + '.key'

                                if len(split_iedb_output_files) > 0:
                                    status_message_with_lock("Parsing IEDB Output for Allele %s and Epitope Length %s - Entries %s" % (a, epl, fasta_chunk), lock)
                                    split_tsv_file_path = "%s_%s" % (self.tsv_file_path(), tsv_chunk)
                                    params = {
                                        'input_iedb_files'       : split_iedb_output_files,
                                        'input_tsv_file'         : split_tsv_file_path,
                                        'key_file'               : split_fasta_key_file_path,
                                        'output_file'            : split_parsed_file_path,
                                    }
                                    if self.additional_report_columns and 'sample_name' in self.additional_report_columns:
                                        params['sample_name'] = self.sample_name
                                    else:
                                        params['sample_name'] = None
                                    parser = self.output_parser(params)
                                    parser.execute()
                                    status_message_with_lock("Parsing IEDB Output for Allele %s and Epitope Length %s - Entries %s - Completed" % (a, epl, fasta_chunk), lock)
                                    split_parsed_output_files.append(split_parsed_file_path)
        return split_parsed_output_files

    def combined_parsed_path(self):
        combined_parsed = "%s.all_epitopes.tsv" % self.sample_name
        return os.path.join(self.output_dir, combined_parsed)

    def combined_parsed_outputs(self, split_parsed_output_files):
        status_message("Combining Parsed IEDB Output Files")
        lib.combine_parsed_outputs.main([
            *split_parsed_output_files,
            self.combined_parsed_path(),
            '--top-score-metric', self.top_score_metric,
        ])
        status_message("Completed")

    def final_path(self):
        return os.path.join(self.output_dir, self.sample_name+".filtered.tsv")

    def ranked_final_path(self):
        return os.path.join(self.output_dir, self.sample_name+".filtered.condensed.ranked.tsv")

    def execute(self):
        self.print_log()
        self.convert_vcf()

        total_row_count = self.tsv_entry_count()
        if total_row_count == 0:
            if self.input_file_type == 'vcf':
                sys.exit("The TSV file is empty. Please check that the input VCF contains missense, inframe indel, or frameshift mutations.")
            elif self.input_file_type == 'bedpe':
                sys.exit("The TSV file is empty. Please check that the input bedpe file contains fusion entries.")
        chunks = self.split_tsv_file(total_row_count)

        self.generate_fasta(chunks)
        split_parsed_output_files = self.call_iedb_and_parse_outputs(chunks)

        if len(split_parsed_output_files) == 0:
            status_message("No output files were created. Aborting.")
            return

        self.combined_parsed_outputs(split_parsed_output_files)

        post_processing_params = vars(self)
        post_processing_params['input_file'] = self.combined_parsed_path()
        post_processing_params['filtered_report_file'] = self.final_path()
        post_processing_params['condensed_report_file'] = self.ranked_final_path()
        if self.input_file_type == 'vcf':
            post_processing_params['run_coverage_filter'] = True
            post_processing_params['run_transcript_support_level_filter'] = True
        else:
            post_processing_params['run_coverage_filter'] = False
            post_processing_params['run_transcript_support_level_filter'] = False
        if self.net_chop_method:
            post_processing_params['run_net_chop'] = True
        else:
            post_processing_params['run_net_chop'] = False
        if self.netmhc_stab:
            post_processing_params['run_netmhc_stab'] = True
        else:
            post_processing_params['run_netmhc_stab'] = False
        PostProcessor(**post_processing_params).execute()

        status_message("\nDone: Pipeline finished successfully. File {} contains list of filtered putative neoantigens.\n".format(self.ranked_final_path()))

        if self.keep_tmp_files is False:
            shutil.rmtree(self.tmp_dir)
