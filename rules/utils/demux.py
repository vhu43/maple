"""
Demultiplexing of BAM files.

Input: BAM file, fasta file of terminal barcodes and/or internal barcodes

Output: Multiple BAM files containing demultiplexed reads, with the file name indicating the distinguishing barcodes.
    If terminal barcodes are not used, output name will be all_X.input.bam, where X is the internal barcode
    If internal barcode is not used, output name will be Y_all.input.bam, where Y is the terminal barcode pair used
"""

import datetime
import multiprocessing as mp
import os
import re
import shutil
import time
import gzip
import itertools

import numpy as np
import pandas as pd
import pysam
from Bio import Align, pairwise2, Seq
from Bio.pairwise2 import format_alignment
from Bio import SeqIO
from collections import Counter
from timeit import default_timer as now
import sys
import tracemalloc

def main():

    ### Asign variables from config file and input
    config = snakemake.config
    tag = snakemake.wildcards.tag
    BAMin = snakemake.input.aln

    ### Output variables
    outputDir = str(snakemake.output.flag).split(f'/.{tag}_demultiplex.done')[0]
    outputStats = snakemake.output.stats
    bcp = BarcodeParser(config, tag)
    bcp.demux_BAM(BAMin, outputDir, outputStats)

class BarcodeParser:

    def __init__(self, config, tag):
        """
        arguments:

        config          - snakemake config dictionary
        tag             - tag for which all BAM files will be demultiplexed, defined in config file
        """
        self.config = config
        self.tag = tag
        self.refSeqfasta = config['runs'][tag]['reference']
        self.reference = list(SeqIO.parse(self.refSeqfasta, 'fasta'))[0]
        self.reference.seq = self.reference.seq.upper()
        self.barcodeInfo = config['runs'][tag]['barcodeInfo']
        self.barcodeGroups = config['runs'][tag]['barcodeGroups'] if 'barcodeGroups' in config['runs'][tag] else {}

    @staticmethod
    def create_barcodes_dict(bcFASTA, revComp):
        """
        creates a dictionary of barcodes from fasta file
        barcodes in fasta must be in fasta form: >barcodeName
                                                  NNNNNNNN
        function will then create a dictionary of these barcodes in the form {NNNNNNNN: barcodeName}
        
        inputs:
            bcFASTA:    string, file name of barcode fasta file used for reference
            revComp:    bool, if set to True, barcode sequences will be stored as reverse complements of the sequences in the fasta file
        """
        bcDict = {}
        for entry in SeqIO.parse(bcFASTA, 'fasta'):
            bcName = entry.id
            bc = str(entry.seq).upper()
            if revComp:
                bc = Seq.reverse_complement(bc)
            bcDict[bc]=bcName
        return bcDict

    @staticmethod
    def find_barcode_context(sequence, context):
        """given a sequence with barcode locations marked as Ns, and a barcode sequence context (e.g. ATCGNNNNCCGA),
        this function will identify the beginning and end of the Ns within the appropriate context if it exists only once.
        If the context does not exist or if the context appears more than once, then will return None as the first output
        value, and the reason why the barcode identification failed as the second value"""

        location = sequence.find(context)
        if location == -1:
            return None, 'barcode context not present in reference sequence'
        N_start = location + context.find('N')
        N_end = location + len(context) - context[::-1].find('N')

        if sequence[N_end:].find(context) == -1:
            return N_start, N_end
        else:
            return None, 'barcode context appears in reference more than once'

    def add_barcode_contexts(self):
        """
        adds dictionary of context strings for each type of barcode provided
        if no context is specified for a barcode type or
        if context cannot be found in the reference sequence, throws an error
        """
        dictOfContexts = {}
        for bcType in self.barcodeInfo:
            try:
                dictOfContexts[bcType] = self.barcodeInfo[bcType]['context'].upper()
                if str(self.reference.seq).find(dictOfContexts[bcType]) == -1:
                    raise ValueError
            except KeyError:
                raise ValueError(f'Barcode type not assigned a sequence context. Add context to barcode type or remove barcode type from barcodeInfo for this tag.\n\nRun tag: `{self.tag}`\nbarcode type: `{bcType}`\nreference sequence: `{self.reference.id}`\nreference sequence fasta file: `{self.refSeqfasta}`')
            except ValueError:
                raise ValueError(f'Barcode context not found in reference sequence. Modify context or reference sequence to ensure an exact match is present.\n\nRun tag: `{self.tag}`\nbarcode type: `{bcType}`\nbarcode sequence context: `{dictOfContexts[bcType]}`\nreference sequence: `{self.reference.id}`\nreference sequence fasta file: `{self.refSeqfasta}`')
        self.barcodeContexts = dictOfContexts

    def add_barcode_dicts(self):
        """
        adds dictionary for each type of barcodes using create_barcodes_dict()
        if no .fasta file is specified for a barcode type, or if the provided fasta file
        cannot be found, throws an error
        """
        dictOfDicts = {}
        for bcType in self.barcodeInfo:
            try:
                fastaFile = self.barcodeInfo[bcType]['fasta']
                if self.barcodeInfo[bcType]['reverseComplement']:
                    RC = True
                else:
                    RC = False
                dictOfDicts[bcType] = self.create_barcodes_dict(fastaFile, RC)
            except KeyError:
                raise ValueError(f'barcode type not assigned a fasta file. Add fasta file to barcode type or remove barcode type from barcodeInfo for this tag.\n\nRun tag: `{self.tag}`\nbarcode type: `{bcType}`\nreference sequence: `{self.reference.id}`')
            except FileNotFoundError:
                fastaFile = self.barcodeInfo[bcType]['fasta']
                raise FileNotFoundError(f'barcode fasta file not found.\n\nRun tag: `{self.tag}`\nbarcode type: `{bcType}`\nreference sequence: `{self.reference.id}`\nfasta file: `{fastaFile}`')
        self.barcodeDicts = dictOfDicts

    @staticmethod
    def hamming_distance(string1, string2):
        """calculates hamming distance, taken from https://stackoverflow.com/questions/54172831/hamming-distance-between-two-strings-in-python"""
        return sum(c1 != c2 for c1, c2 in zip(string1.upper(), string2.upper()))

    @staticmethod
    def contains_duplicates(X):
        return len(set(X)) != len(X)

    def add_barcode_hamming_distance(self):
        """adds barcode hamming distance for each type of barcode (default value = 0), and ensures that
        (1) hamming distances of all possible pairs of barcodes within the fasta file of each barcode
        type are greater than the set hamming distance and (2) that each barcode is the same length as
        the length of Ns in the provided sequence context"""
        hammingDistanceDict = {}
        for barcodeType in self.barcodeDicts:
            if 'hammingDistance' in self.barcodeInfo[barcodeType]:
                hammingDistanceDict[barcodeType] = self.barcodeInfo[barcodeType]['hammingDistance']
            else:
                hammingDistanceDict[barcodeType] = 0 #default Hamming distance of 0
            barcodes = [bc for bc in self.barcodeDicts[barcodeType]]
            context = self.barcodeContexts[barcodeType].upper()
            barcodeLength = context.rindex('N') - context.index('N') + 1
            checkHammingDistances = True
            if len(barcodes)>1000:
                checkHammingDistances = False
                print(f'[NOTICE] More than 1000 barcodes present in barcode fasta file {self.barcodeInfo[barcodeType]["fasta"]}, not checking pairwise hamming distances')
            checkDup = False
            if self.contains_duplicates(barcodes):
                checkDup = True
            for i,bc in enumerate(barcodes):
                if checkDup:
                    assert (barcodes.count(bc) == 1), f'Barcode {bc} present more than once in {self.barcodeInfo[barcodeType]["fasta"]} Duplicate barcodes are not allowed.'
                assert (len(bc) == barcodeLength), f'Barcode {bc} is longer than the expected length of {barcodeLength} based on {self.barcodeContexts[barcodeType]}'
                if checkHammingDistances:
                    otherBCs = barcodes[:i]+barcodes[i+1:]
                    for otherBC in otherBCs:
                        hamDist = self.hamming_distance(bc, otherBC)
                        assert (hamDist > hammingDistanceDict[barcodeType]), f'Barcode {bc} is within hammingDistance {hamDist} of barcode {otherBC}'
        self.hammingDistances = hammingDistanceDict

    @staticmethod
    def hamming_distance_dict(sequence, hamming_distance):
        """given a sequence as a string (sequence) and a desired hamming distance,
        finds all sequences that are at or below the specified hamming distance away
        from the sequence and returns a dictionary where values are the given sequence
        and keys are every sequence whose hamming distance from `sequence` is less
        than or equal to `hamming_distance`"""
        
        hammingDistanceSequences = [[sequence]] # list to be populated with lists of sequences that are the index hamming distance from the first sequence

        for hd in range(0, hamming_distance):
            new_seqs = []
            for seq in hammingDistanceSequences[hd]:
                for i, a in enumerate(seq):
                    if a != sequence[i]: continue
                    for nt in list('ATGC'):
                        if nt == a: continue
                        else: new_seqs.append(seq[:i]+nt+seq[i+1:])
            hammingDistanceSequences.append(new_seqs)

        allHDseqs = list(itertools.chain.from_iterable(hammingDistanceSequences)) # combine all sequences into a single list

        outDict = {}
        for seq in allHDseqs:
            outDict[seq] = sequence

        return outDict

    def add_hamming_distance_barcode_dict(self):
        """Adds a second barcode dictionary based upon hamming distance. For each barcode type, if the
        hamming distance is >0, adds a dictionary where values are barcodes defined in the barcode fasta file,
        and keys are all sequences of the same length with hamming distance from each specific barcode less than
        or equal to the specified hamming distance for that barcode type. Only utilized when a barcode cannot
        be found in the provided barcode fasta file and hamming distance for the barcode type is >0"""
        hammingDistanceBarcodeLookup = {}
        for barcodeType in self.hammingDistances:
            hamDist = self.hammingDistances[barcodeType]
            if hamDist > 0:
                barcodeTypeHDdict = {}
                for barcode in self.barcodeDicts[barcodeType]:
                    barcodeTypeHDdict.update(self.hamming_distance_dict(barcode,hamDist))
                hammingDistanceBarcodeLookup[barcodeType] = barcodeTypeHDdict
        self.hammingDistanceBarcodeDict = hammingDistanceBarcodeLookup

    def find_N_start_end(self, sequence, context):
        """given a sequence with barcode locations marked as Ns, and a barcode sequence context (e.g. ATCGNNNNCCGA),
        this function will return the beginning and end of the Ns within the appropriate context if it exists only once.
        If the context does not exist or if it is uncertain (due to insertions), will return 'fail', 'fail'"""

        location = sequence.find(context)
        if location == -1:
            self.failureReason = 'context_not_present_in_reference_sequence'
            return 'fail', 'fail'
        N_start = location + context.find('N')
        N_end = location + len(context) - context[::-1].find('N')
        if (sequence[max(N_start-1,0)] or sequence[min(N_end+1,len(sequence)-1)]) == '-':
            self.failureReason = 'low_confidence_barcode_identification'
            return 'fail', 'fail'

        return N_start, N_end

    def align_reference(self, BAMentry):
        """given a pysam.AlignmentFile BAM entry,
        builds the reference alignment string with indels accounted for"""
        index = BAMentry.reference_start
        refAln = ''
        for cTuple in BAMentry.cigartuples:
            if cTuple[0] == 0: #match
                refAln += self.reference.seq[index:index+cTuple[1]]
                index += cTuple[1]
            elif cTuple[0] == 1: #insertion
                refAln += '-'*cTuple[1]
            elif cTuple[0] == 2: #deletion
                index += cTuple[1]
        return refAln

    def add_group_barcode_type(self):
        """adds a list of the barcodeTypes that are used for grouping, a list of barcodeTypes that are not
        used for grouping, and a list of barcodes used for sequence ID but not demultiplexing,
        and throws an error if there are different types of groups defined within the barcodeGroups dictionary
        
        Example: if there are 3 barcode types defined in barcodeInfo, fwd, rvs, and alt, and the grouped barcodes
            are defined as {'group1':{'fwd':'barcode1','rvs':'barcode2'},'group2':{'fwd':'barcode3','rvs':'barcode4'}},
            will set self.groupedBarcodeTypes as ['fwd', 'rvs'] and self.ungroupedBarcodeTypes as ['alt']"""

        groupedBarcodeTypes = []
        ungroupedBarcodeTypes = []
        noSplitBarcodeTypes = []
        first = True
        groupDict = {}
        for group in self.barcodeGroups:
            groupDict = self.barcodeGroups[group]
            if first:
                first = False
                firstGroup = group
                firstGroupDict = groupDict
            else:
                assert (all([bcType in groupDict for bcType in firstGroupDict])), f'All barcode groups do not use the same set of barcode types. Group {group} differs from group {firstGroup}'
                assert (all([bcType in firstGroupDict for bcType in groupDict])), f'All barcode groups do not use the same set of barcode types. Group {group} differs from group {firstGroup}'

        for barcodeType in self.barcodeInfo: # add barcode types in the order they appear in barcodeInfo
            if barcodeType in groupDict:
                groupedBarcodeTypes.append(barcodeType)
            elif self.barcodeInfo[barcodeType].get('noSplit', False):
                noSplitBarcodeTypes.append(barcodeType)
            else:
                ungroupedBarcodeTypes.append(barcodeType)

        self.groupedBarcodeTypes = groupedBarcodeTypes
        self.ungroupedBarcodeTypes = ungroupedBarcodeTypes
        self.noSplitBarcodeTypes = noSplitBarcodeTypes

    def add_barcode_name_dict(self):
        """adds the inverse of dictionary of barcodeGroups, where keys are tuples of
        barcode names in the order specified in barcodeInfo, and values are group names, as defined in barcodeGroups.
        used to name files that contain sequences containing specific barcode combinations"""

        groupNameDict = {}

        for group in self.barcodeGroups:
            groupDict = self.barcodeGroups[group]
            key = tuple()
            for barcodeType in self.groupedBarcodeTypes:
                if barcodeType in groupDict:
                    key += tuple([groupDict[barcodeType]])
            groupNameDict[key] = group

        self.barcodeGroupNameDict = groupNameDict

    def get_demux_output_prefix(self, sequenceBarcodesDict):
        """given a dictionary of barcode names for a particular sequence, as specified in the barcodeInfo config dictionary,
        uses the barcode group name dictionary `self.barcodeGroupNameDict` created by add_barcode_name_dict
        to generate a file name prefix according to their group name if it can be identified, or will
        produce a file name prefix simply corresponding to the barcodes if a group can't be identified.

        examples:
        
        1)

        if the order of barcodes in barcodeInfo is 'fwd','rvs','alt' and 
        barcodeNamesDict == {'fwd':'one', 'rvs':'two', 'alt':'three'} and self.barcodeGroups == {'group1':{'fwd':'one','rvs':'two'}}:

        self.get_demux_output_prefix(barcodeNamesList) will return 'group1-three'
        

        2)

        if the order of barcodes in barcodeInfo is 'fwd','rvs','alt' and 
        barcodeNamesList == ['one', 'two', 'three'] and self.barcodeGroups == {'group1':{'fwd':'two','rvs':'two'}}:

        self.get_demux_output_prefix(barcodeNamesList) will return 'one-two-three'
        """
        # split barcodes into those utilized by groups and those not utilized by groups. Group tuple used as dictionary key to look up group name
        ungroupSequenceBarcodes = []
        groupBarcodes = tuple()

        for barcodeType in sequenceBarcodesDict:
            bc = sequenceBarcodesDict[barcodeType]
            if barcodeType in self.groupedBarcodeTypes:
                if bc == 'fail':
                    return '-'.join(sequenceBarcodesDict.values()), False
                groupBarcodes += tuple([bc])
            else:
                ungroupSequenceBarcodes.append(bc)

        groupName = self.barcodeGroupNameDict.get(groupBarcodes, False)
        if groupName:
            out = ('-'.join([groupName]+ungroupSequenceBarcodes), True)
        else:
            out = ('-'.join(sequenceBarcodesDict.values()), False)
        if out[0] == '':
            out = ('all', True)      
        return out


    def id_seq_barcodes(self, refAln, BAMentry):
        """Inputs:
            refAln:         aligned reference string from align_reference()
            BAMentry:       pysam.AlignmentFile entry
        
        Returns a list of information for a provided `BAMentry` that will be used for demuxing
        and will be added as a row for a demux stats DataFrame"""
        
        sequenceBarcodesDict = {}
        barcodeNames = []
        bcDataList = []
        
        for barcodeType in self.barcodeDicts:

            self.failureReason = None
            barcodeName = None
            notExactMatch = 0
            failureReason = {'context_not_present_in_reference_sequence':0, 'barcode_not_in_fasta':0, 'low_confidence_barcode_identification':0}
            start,stop = self.find_N_start_end(refAln, self.barcodeContexts[barcodeType])

            if type(start)==int:
                barcode = BAMentry.query_alignment_sequence[ start:stop ]
            else:
                barcodeName = 'fail'
                failureReason[self.failureReason] = 1 # failure reason is set by self.find_N_start_end
            
            if barcodeName != 'fail':
                if barcode in self.barcodeDicts[barcodeType]:
                    barcodeName = self.barcodeDicts[barcodeType][barcode]
                else:
                    if barcodeType in self.hammingDistanceBarcodeDict:
                        if barcode in self.hammingDistanceBarcodeDict[barcodeType]:
                            closestBarcode = self.hammingDistanceBarcodeDict[barcodeType][barcode]
                            barcodeName = self.barcodeDicts[barcodeType][closestBarcode]
                            notExactMatch = 1
                        else:
                            barcodeName = 'fail'
                            failureReason['barcode_not_in_fasta'] = 1
                    else:
                        barcodeName = 'fail'
                        failureReason['barcode_not_in_fasta'] = 1
            
            sequenceBarcodesDict[barcodeType] = barcodeName
            barcodeNames += [barcodeName]
            bcDataList += [notExactMatch] + list(failureReason.values())

        return sequenceBarcodesDict, barcodeNames, np.array(bcDataList)


    def demux_BAM(self, BAMin, outputDir, outputStats):
        bamfile = pysam.AlignmentFile(BAMin, 'rb')
        self.add_barcode_contexts()
        self.add_barcode_dicts()
        self.add_barcode_hamming_distance()
        self.add_hamming_distance_barcode_dict()
        self.add_group_barcode_type()
        self.add_barcode_name_dict()

        # dictionary where keys are file name parts indicating the barcodes and values are file objects corresponding to those file name parts
            # file objects are only created if the specific barcode combination is seen
        outFileDict = {}
        
        # columns names for dataframe to be generated from rows output by id_seq_barcodes
        colNames = ['tag', 'output_file_barcodes', 'named_by_group']

        # column names and dictionary for grouping rows in final dataframe
        groupByColNames = ['tag', 'output_file_barcodes', 'named_by_group', 'demuxed_count']
        sumColsDict = {'barcodes_count':'sum'}

        barcodeFailureColNames = []
        for barcodeType in self.barcodeDicts:
            groupByColNames.append(barcodeType)
            intCols = [f'{barcodeType}:not_exact_match', f'{barcodeType}_failed:context_not_present_in_reference_sequence', f'{barcodeType}_failed:barcode_not_in_fasta', f'{barcodeType}_failed:low_confidence_barcode_identification']
            colNames.append(barcodeType)
            barcodeFailureColNames.extend(intCols)
            for col in intCols:
                sumColsDict[col] = 'sum'
        colNames.extend( ['barcodes_count'] + barcodeFailureColNames)

        os.makedirs(outputDir, exist_ok=True)
        rowCountsDict = Counter()
        for BAMentry in bamfile.fetch(self.reference.id):
            refAln = self.align_reference(BAMentry)
            sequenceBarcodesDict, barcodeNames, bcDataArray = self.id_seq_barcodes(refAln, BAMentry) #this takesd about 3 times as long as other steps in this loop, probably due to try except clauses
            if len(self.noSplitBarcodeTypes) > 0:
                noSplitBarcodeBAMtag = '_'.join([sequenceBarcodesDict.pop(noSplitBarcode) for noSplitBarcode in self.noSplitBarcodeTypes])
                BAMentry.set_tag('BC', noSplitBarcodeBAMtag)
            outputBarcodes, groupedBool = self.get_demux_output_prefix(sequenceBarcodesDict)
            if not outFileDict.get(outputBarcodes, False):
                fName = os.path.join(outputDir, f'{self.tag}_{outputBarcodes}.bam')
                outFileDict[outputBarcodes] = pysam.AlignmentFile(fName, 'wb', template=bamfile)
            
            outFileDict[outputBarcodes].write(BAMentry)
            bcDataArray = np.insert(bcDataArray, 0, 1)                                                                    # insert 1 in front to serve as counter for total number of sequences with these barcodes
            rowCountsDict[tuple([self.tag, outputBarcodes, groupedBool] + barcodeNames)] += bcDataArray     # add counters for demux and data on failure modes
            
        # combine barcode info (strings) and counters (int) from dict into a list of row lists
        rows = []
        for row, counters in rowCountsDict.items():
            row = list(row)
            counters = counters.tolist()
            rows.append(row + counters)

        for sortedBAM in outFileDict:
            outFileDict[sortedBAM].close()
            
        # add counts for both number of sequences in file as well as number of sequences with same exact barcodes
        demuxStats = pd.DataFrame(rows, columns=colNames)
        totalSeqs = demuxStats['barcodes_count'].sum()
        fileCounts = demuxStats[['output_file_barcodes','barcodes_count']].groupby('output_file_barcodes').sum().squeeze()
        fileCountsCol = demuxStats.apply(lambda x:
            fileCounts[x['output_file_barcodes']], axis=1)
        demuxStats.insert(2, 'demuxed_count', fileCountsCol)
        demuxStats = demuxStats.groupby(groupByColNames).agg(sumColsDict).reset_index()
        demuxStats.sort_values(['demuxed_count','barcodes_count'], ascending=False, inplace=True)

        # move files with sequence counts below the set threshold or having failed any barcodes to a subdirectory
        banishDir = os.path.join(outputDir, 'no_subsequent_analysis')
        os.makedirs(banishDir, exist_ok=True)
        banished = [] # prevent banishing same file more than once
        for i, row in demuxStats.iterrows():
            if row['output_file_barcodes'] in banished: continue
            banish = False
            if row['demuxed_count'] < self.config['demux_threshold'] * totalSeqs:
                banish = True
            if self.config.get('demux_screen_failures', False):
                for barcodeType in self.groupedBarcodeTypes+self.ungroupedBarcodeTypes:
                    if row[barcodeType] == 'fail':
                        banish = True
            if (self.config.get('demux_screen_no_group', False) == True) and not row['named_by_group']:
                banish = True
            if banish:
                original = os.path.join(outputDir, f"{self.tag}_{row['output_file_barcodes']}.bam")
                target = os.path.join(banishDir, f"{self.tag}_{row['output_file_barcodes']}.bam")
                shutil.move(original, target)
                banished.append(row['output_file_barcodes'])

        demuxStats.drop('named_by_group', axis=1, inplace=True)
        demuxStats.to_csv(outputStats, index=False)


if __name__ == '__main__':
    main()