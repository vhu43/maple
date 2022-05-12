from Bio.Seq import Seq
from Bio import SeqIO
import Bio
import numpy as np
import pandas as pd
import re
import pysam

### Asign variables from config file
config = snakemake.config
BAMin = str(snakemake.input.bam)
tag = BAMin.strip('.bam').split('/')[1].split('_')[0]
###

outputDir = 'mutation_data'

def main():
    x = MutationAnalysis(config, tag, BAMin, snakemake.output)
    x.process_seqs()

class MutationAnalysis:

    @staticmethod
    def longest_ORF(sequence):
        return max(re.findall(r'ATG(?:(?!TAA|TAG|TGA)...)*(?:TAA|TAG|TGA)', sequence), key = len) # taken from https://stackoverflow.com/questions/31757876/python-find-longest-orf-in-dna-sequence#31758161

    def __init__(self, config, tag, BAMin, output):
        """
        arguments:

        config          - snakemake config dictionary for all runs
        tag             - tag for which all BAM files will be demultiplexed, defined in config file
        BAMin           - BAM file input
        output          - list of output file names
        """
        refSeqfasta = config['runs'][tag]['reference']
        self.ref = list(SeqIO.parse(refSeqfasta, 'fasta'))[0]
        self.ref.seq = self.ref.seq.upper()
        self.refTrimmed = list(SeqIO.parse(refSeqfasta, 'fasta'))[1]
        self.refTrimmed.seq = self.refTrimmed.seq.upper()
        self.refStr = str(self.ref.seq)
        self.refTrimmedStr = str(self.refTrimmed.seq)
        self.config = config
        self.highestAbundanceGenotypes = config['highest_abundance_genotypes']
        self.BAMin = BAMin
        self.outputList = output
        self.refTrimmedStart = self.refStr.find(self.refTrimmedStr)
        self.refTrimmedEnd = len(self.refTrimmedStr) + self.refTrimmedStart

        if 'mutation_analysis_quality_score_minimum' in config:
            self.QSminimum = config['mutation_analysis_quality_score_minimum']
        self.doAAanalysis = config['do_AA_mutation_analysis'][tag]

        if self.doAAanalysis: # define protein sequence
            refProtein = list(SeqIO.parse(refSeqfasta, 'fasta'))[2]
            refProteinStr = str(refProtein.seq).upper()
            self.refProtein = refProteinStr
            self.refProteinStart = self.refTrimmedStr.find(self.refProtein)
            self.refProteinEnd = len(self.refProtein) + self.refProteinStart

        self.AAs = "ACDEFGHIKLMNPQRSTVWY*"
        self.NTs = "ATGC"


    def clean_alignment(self, BAMentry):
        """given a pysam.AlignmentFile BAM entry,
        trims the ends off the query and reference sequences according to the trimmed reference,
        aligns these two strings as well as the quality scores, and creates an alignment string between the
        two trimmed sequences  where '|'=match, and '.'=mismatch, and returns these three strings, the list of quality
        scores, a list of all insertions, and a list of all deletions
        """

        if BAMentry.reference_name != self.ref.id:
            self.alignmentFailureReason = ('alignment uses wrong reference sequence', 'N/A')
            return None
        
        if BAMentry.reference_start > self.refTrimmedStart:
            self.alignmentFailureReason = ('alignment starts past trimmed reference start', 'N/A')
            return None

        if BAMentry.reference_end < self.refTrimmedEnd:
            self.alignmentFailureReason = ('alignment ends before trimmed reference end', 'N/A')
            return None

        refIndex = BAMentry.reference_start
        queryIndex = 0
        refAln = ' ' * refIndex  #pad beginning of alignment when alignment begins after beginning of reference
        queryAln = ' ' * refIndex
        queryQualities = [-1] * refIndex if self.fastq else None
        alignStr = ' ' * refIndex
        insertions = [] # list of tuples where first element is index within trimmed reference, second element is sequence inserted
        deletions = []  # list of tuples where first element is index within trimmed reference, second element is number of bases deleted

        for cTuple in BAMentry.cigartuples: # build pretty alignment through combining consecutive segments, which are determined by the cigar tuples, in which the first value determines the operation and the second value determines the length

            if cTuple[0] == 0: #no indel
                refSegment = self.refStr[refIndex:refIndex+cTuple[1]]
                querySegment = BAMentry.query_alignment_sequence[queryIndex:queryIndex+cTuple[1]]
                alignSegment = ''.join(['|' if r==q else '.' for r,q in zip(refSegment,querySegment)])
                refAln += refSegment
                queryAln += querySegment
                if self.fastq:
                    queryQualities += BAMentry.query_alignment_qualities[queryIndex:queryIndex+cTuple[1]]
                alignStr += alignSegment
                refIndex += cTuple[1]
                queryIndex += cTuple[1]

            elif cTuple[0] == 1: #insertion, not added to sequence to maintain alignment to reference
                if self.doAAanalysis and not self.config['analyze_seqs_w_frameshift_indels'] and cTuple[1]%3 != 0 and self.refProteinStart <= refIndex < self.refProteinEnd: # frameshift, discard sequence if protein sequence analysis is being done and indel sequences are being ignored
                    self.alignmentFailureReason = ('frameshift insertion', queryIndex)
                    return None
                if self.refTrimmedStart <= refIndex < self.refTrimmedEnd:
                    insertions.append((refIndex-self.refTrimmedStart, BAMentry.query_alignment_sequence[queryIndex:queryIndex+cTuple[1]]))
                queryIndex += cTuple[1]

            elif cTuple[0] == 2: #deletion, '-' added to sequence to maintain alignment to reference
                if self.doAAanalysis and not self.config['analyze_seqs_w_frameshift_indels'] and cTuple[1]%3 != 0 and self.refProteinStart <= refIndex < self.refProteinEnd: # frameshift, discard sequence if protein sequence analysis is being done and indel sequences are being ignored
                    self.alignmentFailureReason = ('frameshift deletion', queryIndex)
                    return None
                refAln += self.refStr[refIndex:refIndex+cTuple[1]]
                queryAln += '-'*cTuple[1]
                alignStr += ' '*cTuple[1]
                if self.fastq:
                    queryQualities += [0]*cTuple[1]
                if self.refTrimmedStart <= refIndex < self.refTrimmedEnd:
                    deletions.append((refIndex-self.refTrimmedStart, cTuple[1]))
                refIndex += cTuple[1]

        return  [refAln[self.refTrimmedStart:self.refTrimmedEnd],
                alignStr[self.refTrimmedStart:self.refTrimmedEnd],
                queryAln[self.refTrimmedStart:self.refTrimmedEnd],
                queryQualities[self.refTrimmedStart:self.refTrimmedEnd] if self.fastq else None,
                insertions,
                deletions]


    def ID_muts(self, cleanAlignment):
        """ Identify mutations in an aligned sequence

        Input: output of clean_alignment(). Mutations only counted if quality score
        is above globally set threshold.

        Outputs:
            ntMutArray  - x by y array where x is the length of self.refTrimmed, representing each
                            nucleotide position within this sequence, and y is four, representing the
                            possible nucleotides that a base could be mutated to. values in the array
                            will be set to 1 if a mutation at position x is mutated to nucleotide that
                            corresponds to position y. If no mutations are found, an array of 0s is returned 
            aaMutArray  - x by y array where x is the length of self.refORF, representing each
                            amino acid position within this sequence, and y is the length of self.AAs,
                            representing the number of
                            possible nucleotides that a base could be mutated to. values in the array
                            will be set to 1 if a mutation at position x is mutated to amino acid that
                            corresponds to position y. If no mutations are found, an array of 0s is returned
            genotype    - list of strings where each string is a list of different types of mutations separated by ', '.
                            Includes AA mutations in list only if self.doAAanalysis is True
        """
        ref, alignStr, seq, qScores, insertions, deletions = cleanAlignment

        mismatches = [i for i,a in enumerate(alignStr) if a=='.']

        if self.doAAanalysis:
            indelCodons = []    # list of amino acid positions that are affected by indel (for insertion, insertion is within a codon; for deletion, at least one base of codon deleted)
            for index, _ in insertions:
                if self.refProteinStart <= index < self.refProteinEnd:
                    protIndex = index-self.refProteinStart
                    if protIndex%3 == 0: continue # ignore if insertion occurs between codons
                    else: indelCodons.append( int(protIndex/3) )

            for index, length in deletions:
                if (self.refProteinStart <= index < self.refProteinEnd) or (self.refProteinStart <= index+length < self.refProteinEnd):
                    protIndexStart = index-self.refProteinStart
                    protIndexEnd = (index+length)-self.refProteinStart
                    firstCodon = int(protIndexStart/3)
                    lastCodon = int(protIndexEnd/3)
                    indelCodons.extend([i for i in range(firstCodon,lastCodon+1)])
            self.indelCodons = indelCodons
            AAmutArray = np.zeros((int(len(self.refProtein)/3), len(self.AAs)), dtype=int)
        else:
            AAmutArray = None

        insOutput = ', '.join([str(index)+'ins'+NTs for index,NTs in insertions])               # string of all insertions for genotype output
        delOutput = ', '.join([str(index)+'del'+str(length) for index,length in deletions])     # string of all deletions for genotype output

        NTmutArray = np.zeros((int(len(self.refTrimmedStr)), len(self.NTs)), dtype=int)
        codonsChecked = []
        NTsubstitutions = []
        AAnonsynonymous = []
        AAsynonymous = []

        for i in mismatches:

            if self.fastq:
                if qScores[i] < self.QSminimum: continue

            wtNT = ref[i]
            mutNT = seq[i]

            NTmutArray[i,self.NTs.find(mutNT)] += 1
            NTsubstitutions.append(wtNT+str(i)+mutNT)

            if self.doAAanalysis and self.refProteinStart <= i < self.refProteinEnd:

                protIndex = i-self.refProteinStart
                codon = int(protIndex/3) # 0-index amino acid position
                if codon in codonsChecked: continue
                codonsChecked.append(codon)
                codonPosi = protIndex%3
                codonIndices = list(range(i-codonPosi, i+(3-codonPosi)))

                # check that codon doesn't contain any bases influenced by an indel
                if codon in indelCodons: continue

                #check that all three quality scores in codon are above threshold
                if qScores:
                    QStooLow = False
                    codonQS = qScores[codonIndices[0]:codonIndices[2]]
                    for qs in codonQS:
                        if qs < self.QSminimum:
                            QStooLow = True
                    if QStooLow: continue

                wtAA = str(Seq(ref[codonIndices[0]:codonIndices[2]+1]).translate())
                mutAA = str(Seq(seq[codonIndices[0]:codonIndices[2]+1]).translate())

                if wtAA!=mutAA:
                    AAmutArray[codon, self.AAs.find(mutAA)] += 1
                    AAnonsynonymous.append(wtAA+str(codon+1)+mutAA)
                else:
                    AAsynonymous.append(wtAA+str(codon+1))

        genotype = [', '.join(NTsubstitutions)]
        genotype.append(len(NTsubstitutions))
        genotype.append(insOutput)
        genotype.append(delOutput)

        if self.doAAanalysis:
            for subType in [AAnonsynonymous, AAsynonymous]:
                genotype.append(', '.join(subType))
            genotype.append(len(AAnonsynonymous))
        else:
            totalAAmuts = None
            
        return NTmutArray, AAmutArray, genotype

    def process_seqs(self):
        """loops through a BAM file and produces appropriate .csv files to describe mutation data.
        If config['do_AA_analysis']==False, will produce only files for NT mutation data, otherwise
        will also produce AA mutation data"""

        trimmedSeqLength = int(len(self.refTrimmedStr))
        NTmutArray = np.zeros((trimmedSeqLength, len(self.NTs)), dtype=int)         # see ID_muts() docstring
        NTmutDist = np.zeros(trimmedSeqLength, dtype=int)                           # 1D array distribution where position (x) is number of mutations and value is number of sequences with x mutations 
        failuresList = []                                                           # list of data for failed sequences that will be used to generate DataFrame
        failuresColumns = ['seq_ID', 'failure_reason', 'failure_index']             # columns for failures DataFrame
        genotypesList = []                                                          # list of genotypes to be converted into a DataFrame
        genotypesColumns = ['seq_ID', 'avg_quality_score', 'NT_substitutions', 'NT_substitutions_count', 'NT_insertions', 'NT_deletions'] # columns for genotypes DataFrame
        wildTypeCount = 0
        wildTypeRow = [wildTypeCount, 0, '', 0, '', '']

        if self.doAAanalysis:
            protLength = int( len(self.refProtein) / 3 )
            AAmutArray = np.zeros((protLength, len(self.AAs)), dtype=int)
            AAmutDist = np.zeros(protLength, dtype=int)
            genotypesColumns.extend(['AA_substitutions_nonsynonymous', 'AA_substitutions_synonymous', 'AA_substitutions_nonsynonymous_count'])
            wildTypeRow.extend(['', '', 0])

        # if any barcodes are not used to demultiplex, add a column that shows what these barcodes are
        self.barcodeColumn = False
        if self.config['do_demux'][tag]:
            for bcType in self.config['runs'][tag]['barcodeInfo']:
                if self.config['runs'][tag]['barcodeInfo'][bcType].get('noSplit', False):
                    self.barcodeColumn = True
                    genotypesColumns.append('barcode(s)')
                    wildTypeRow.append('')

        # if there are any mutations of interest for this tag, add genotype columns for these
        if self.config['runs'][tag].get('NT_muts_of_interest', False):
            genotypesColumns.append('NT_muts_of_interest')
            wildTypeRow.append('')
            self.NT_muts_of_interest = self.config['runs'][tag]['NT_muts_of_interest'].split(', ')
            for mut in self.NT_muts_of_interest:
                genotypesColumns.append(mut)
                wildTypeRow.append(0)
        if self.doAAanalysis and self.config['runs'][tag].get('AA_muts_of_interest', False):
            genotypesColumns.append('AA_muts_of_interest')
            wildTypeRow.append('')
            self.AA_muts_of_interest = self.config['runs'][tag]['AA_muts_of_interest'].split(', ')
            for mut in self.AA_muts_of_interest:
                genotypesColumns.append(mut)
                wildTypeRow.append(0)

        bamFile = pysam.AlignmentFile(self.BAMin, 'rb')

        # set whether to use quality score features based on whether or not quality scores are present
        for bamEntry in bamFile:
            self.fastq = False
            if bamEntry.query_alignment_qualities:
                self.fastq = True
            bamFile.reset()
            break

        for bamEntry in bamFile:
            cleanAln = self.clean_alignment(bamEntry)
            if cleanAln:
                seqNTmutArray, seqAAmutArray, seqGenotype = self.ID_muts(cleanAln)                    
            else:
                failuresList.append([bamEntry.query_name, self.alignmentFailureReason[0], self.alignmentFailureReason[1]])
                continue

            seqTotalNTmuts = sum(sum(seqNTmutArray))
            NTmutArray += seqNTmutArray
            NTmutDist[seqTotalNTmuts] += 1
            if self.doAAanalysis:
                AAmutArray += seqAAmutArray
                seqTotalAAmuts = sum(sum(seqAAmutArray))
                AAmutDist[seqTotalAAmuts] += 1

            if all(mutType=='' for mutType in seqGenotype) and not self.barcodeColumn:     # keep a counter for wild type sequences instead of adding them to genotypes dataframe. 'wildtype' doesn't make sense if using noSplit barcodes, so this will be skipped and there will not be a wildtype row
                wildTypeCount += 1
                continue

            if not self.fastq:
                avgQscore = -1
            else:
                avgQscore = np.average(np.array(cleanAln[3]))
            seqGenotype = [bamEntry.query_name, avgQscore] + seqGenotype

            if self.barcodeColumn:
                seqGenotype.append(bamEntry.get_tag('BC'))

            if self.config['runs'][tag].get('NT_muts_of_interest', False):
                mutStr = ''
                mutOneHot = []
                for mut in self.NT_muts_of_interest:
                    if mut in seqGenotype[genotypesColumns.index('NT_substitutions')].split(', '):
                        mutStr += mut
                        mutOneHot.append(1)
                    else:
                        mutOneHot.append(0)
                seqGenotype.append(mutStr)
                seqGenotype.extend(mutOneHot)

            if self.doAAanalysis and self.config['runs'][tag].get('AA_muts_of_interest', False):
                mutStr = ''
                mutOneHot = []
                for mut in self.AA_muts_of_interest:
                    if mut in seqGenotype[genotypesColumns.index('AA_substitutions_nonsynonymous')].split(', '):
                        mutStr += mut
                        mutOneHot.append(1)
                    else:
                        mutOneHot.append(0)
                seqGenotype.append(mutStr)
                seqGenotype.extend(mutOneHot)
            

            genotypesList.append(seqGenotype)

        genotypesDF = pd.DataFrame(genotypesList, columns=genotypesColumns)
        failuresDF = pd.DataFrame(failuresList, columns=failuresColumns)
        
        genotypesDFcondensed = genotypesDF.groupby(by=genotypesColumns[2:], as_index=False).agg({'seq_ID':'count', 'avg_quality_score':'max'})[list(genotypesDF.columns)]
        genotypesDFcondensed.sort_values(['seq_ID'], ascending=False, ignore_index=True, inplace=True)
        wildTypeRow[0] = wildTypeCount
        wildTypeDF = pd.DataFrame([wildTypeRow], columns=genotypesColumns)
        genotypesDFcondensed = pd.concat([wildTypeDF,genotypesDFcondensed], ignore_index=True)
        genotypesDFcondensed.rename(index={0:'wildtype'}, inplace=True)
        if self.barcodeColumn:
            genotypesDFcondensed.drop(index='wildtype', inplace=True)
        genotypesDFcondensed.reset_index(inplace=True)
        genotypesDFcondensed.rename(columns={'index':'genotype', 'seq_ID':'count'}, inplace=True)

        # iterate through x genotypes with highest counts (x defined in config file under 'num_representative_seqs'), get the representative sequence, and write alignments to file
        topHitsDF = genotypesDFcondensed.iloc[0:self.highestAbundanceGenotypes+1,]
        with open(self.outputList[0], 'w') as txtOut:
            nameIndexedBAM = pysam.IndexedReads(bamFile)
            nameIndexedBAM.build()
            for row in topHitsDF.itertuples():
                if row.genotype=='wildtype':
                    continue
                rowIndexFromBool = (row.avg_quality_score == genotypesDF.loc[:, 'avg_quality_score']) & (row.NT_substitutions == genotypesDF.loc[:, 'NT_substitutions']) & (row.NT_insertions == genotypesDF.loc[:, 'NT_insertions']) & (row.NT_deletions == genotypesDF.loc[:, 'NT_deletions'])
                seqID = genotypesDF[rowIndexFromBool]['seq_ID'].tolist()[0]
                iterator = nameIndexedBAM.find(seqID)
                for BAMentry in iterator:
                    break
                x = self.clean_alignment(BAMentry)
                if x == None: print(BAMentry.query_name)
                ref, alignString, seq, _, _, _ = x
                txtOut.write(f'Genotype {row.genotype} representative sequence. Sequence ID: {seqID}\n')
                for string in [ref, alignString, seq]:
                    txtOut.write(string+'\n')
                txtOut.write('\n')

        ntIDs = list(self.refTrimmedStr)
        ntPositions = [f'{str(i)}' for i in range(0, len(self.refTrimmedStr))]
        WTnts = [ID+ntPosi for ID,ntPosi in zip(ntIDs,ntPositions)]
        NTmutDF = pd.DataFrame(NTmutArray, columns=list(self.NTs))
        NTmutDF['wt_nucleotides'] = pd.Series(WTnts)
        NTmutDF.set_index('wt_nucleotides', inplace=True)
        NTmutDF = NTmutDF.transpose()

        totalSeqs = int(NTmutDist.sum())

        NTdistDF = pd.DataFrame(NTmutDist, columns=['seqs_with_n_NTsubstitutions'])
        NTdistDF.index.name = 'n'

        genotypesDFcondensed.drop(columns=['avg_quality_score'], inplace=True)

        genotypesDFcondensed.to_csv(self.outputList[1], index=False)
        failuresDF.to_csv(self.outputList[2], index=False)
        NTmutDF.index.name = 'NT_mutation_count'
        if not self.config['mutations_frequencies_raw'] and totalSeqs>0:
            NTmutDF = NTmutDF.divide(totalSeqs)
        NTmutDF.to_csv(self.outputList[3])
        NTdistDF.to_csv(self.outputList[4])
        
        if self.doAAanalysis:
            resiIDs = list(str(Seq(self.refProtein).translate()))
            protLength = int(len(self.refProtein)/3)
            resiPositions = [str(i) for i in range(1, int((len(self.refProtein)/3)+1) )]
            WTresis = [ID+posi for ID,posi in zip(resiIDs,resiPositions)]
            AAmutDF = pd.DataFrame(AAmutArray, columns=list(self.AAs))
            AAmutDF['wt_residues'] = pd.Series(WTresis)
            AAmutDF.set_index('wt_residues', inplace=True)
            AAmutDF = AAmutDF.transpose()
            AAmutDF.index.name = 'AA_mutation_count'
            if not self.config['mutations_frequencies_raw'] and totalSeqs > 0:
                AAmutDF = AAmutDF.divide(totalSeqs)
            AAmutDF.to_csv(self.outputList[5])

            AAdistDF = pd.DataFrame(AAmutDist, columns=['seqs_with_n_AAsubstitutions'])
            AAdistDF.index.name = 'n'
            AAdistDF.to_csv(self.outputList[6])

if __name__ == '__main__':
    main()