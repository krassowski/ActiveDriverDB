import gzip
from collections import defaultdict, namedtuple
from warnings import warn

from tqdm import tqdm
from database import db, create_key_model_dict
from database import get_or_create
from helpers.bioinf import aa_symbols
from helpers.parsers import parse_fasta_file, iterate_tsv_gz_file
from helpers.parsers import parse_tsv_file
from helpers.parsers import parse_text_file
from imports.importer import create_simple_importer, BioImporter
from models import (
    Domain, UniprotEntry, MC3Mutation, InheritedMutation, Mutation, Drug, DrugGroup, DrugType, SiteType,
    SiteMotif,
)
from models import Gene
from models import InterproDomain
from models import Cancer
from models import Kinase
from models import KinaseGroup
from models import Protein
from models import Pathway
from models import GeneList
from models import GeneListEntry


def get_proteins(cached_proteins={}, reload_cache=False):
    """Fetch all proteins from database as refseq => protein object mapping.

    By default proteins will be cached at first call and until cached_proteins
    is set explicitly to a (new, empty) dict() in subsequent calls, the
    cached results from the first time will be returned."""
    if reload_cache:
        cached_proteins.clear()
    if not cached_proteins:
        for protein in Protein.query:
            cached_proteins[protein.refseq] = protein
    return cached_proteins


importer = create_simple_importer(BioImporter)


@importer
def proteins_and_genes(path='data/protein_data.tsv'):
    """Create proteins and genes based on data in a given file.

    If protein/gene already exists it will be skipped.

    Returns:
        list of created (new) proteins
    """
    # TODO where does the tsv file come from?
    print('Creating proteins and genes:')

    genes = create_key_model_dict(Gene, 'name', lowercase=True)
    known_proteins = get_proteins()

    proteins = {}

    coordinates_to_save = [
        ('txStart', 'tx_start'),
        ('txEnd', 'tx_end'),
        ('cdsStart', 'cds_start'),
        ('cdsEnd', 'cds_end')
    ]

    allowed_strands = ['+', '-']

    # a list storing refseq ids which occur at least twice in the file
    with_duplicates = []
    potentially_empty_genes = set()

    header = [
        'bin', 'name', 'chrom', 'strand', 'txStart', 'txEnd',
        'cdsStart', 'cdsEnd', 'exonCount', 'exonStarts', 'exonEnds',
        'score', 'name2', 'cdsStartStat', 'cdsEndStat', 'exonFrames'
    ]

    columns = tuple(header.index(x[0]) for x in coordinates_to_save)
    coordinates_names = [x[1] for x in coordinates_to_save]

    def parser(line):

        # use name2 (fourth column from the end)
        name = line[-4]

        strand = line[3]
        assert strand in allowed_strands

        gene_data = {
            'name': name,
            'chrom': line[2][3:],    # remove chr prefix
            'strand': True if strand == '+' else False
        }

        if name.lower() not in genes:
            gene = Gene(**gene_data)
            genes[name.lower()] = gene
        else:
            gene = genes[name.lower()]
            for key, value in gene_data.items():
                previous = getattr(gene, key)
                if previous != value:
                    print(f'Replacing {gene} {key} with {value} (previously: {previous})')
                    setattr(gene, key, value)

        # load protein
        refseq = line[1]

        # if protein is already in database no action is required
        if refseq in known_proteins:
            return

        # do not allow duplicates
        if refseq in proteins:

            with_duplicates.append(refseq)
            potentially_empty_genes.add(gene)

            """
            if gene.chrom in ('X', 'Y'):
                # close an eye for pseudoautosomal regions
                print(
                    'Skipping duplicated entry (probably belonging',
                    'to pseudoautosomal region) with refseq:', refseq
                )
            else:
                # warn about other duplicated records
                print(
                    'Skipping duplicated entry with refseq:', refseq
                )
            """
            return

        # from this line there is no processing of duplicates allowed
        assert refseq not in proteins

        protein_data = {'refseq': refseq, 'gene': gene}

        coordinates = zip(
            coordinates_names,
            [
                int(value)
                for i, value in enumerate(line)
                if i in columns
            ]
        )
        protein_data.update(coordinates)

        proteins[refseq] = Protein(**protein_data)

    parse_tsv_file(path, parser, header)

    cnt = sum(map(lambda g: len(g.isoforms) == 1, potentially_empty_genes))
    print('Duplicated that are only isoforms for gene:', cnt)
    print('Duplicated rows detected:', len(with_duplicates))
    return proteins.values()


@importer
def sequences(path='data/all_RefGene_proteins.fa'):
    proteins = get_proteins()

    print('Loading sequences:')

    overwritten = 0
    new_count = 0

    def on_header(refseq):
        nonlocal overwritten, new_count

        assert refseq in proteins

        if proteins[refseq].sequence:
            proteins[refseq].sequence = ''
            overwritten += 1
        else:
            new_count += 1

        return refseq

    def on_sequence(refseq, line):
        proteins[refseq].sequence += line

    parse_fasta_file(path, on_sequence, on_header)

    print('%s sequences overwritten' % overwritten)
    print('%s new sequences saved' % new_count)


@importer
def protein_summaries(path='data/refseq_summary.tsv.gz'):

    known_proteins = get_proteins()

    expected_header = ['#mrnaAcc', 'completeness', 'summary']

    for line_data in iterate_tsv_gz_file(path, expected_header):

        try:
            refseq, completeness, summary = line_data
        except ValueError:
            continue

        if refseq not in known_proteins:
            continue

        known_proteins[refseq].summary = summary


@importer
def external_references(path='data/HUMAN_9606_idmapping.dat.gz', refseq_lrg='data/LRG_RefSeqGene', refseq_link='data/refseq_link.tsv.gz'):
    from models import Protein
    from models import ProteinReferences
    from models import EnsemblPeptide
    from sqlalchemy.orm.exc import NoResultFound

    references = defaultdict(list)

    def add_uniprot_accession(data):

        # full uniprot includes isoform (if relevant)
        full_uniprot, ref_type, value = data

        if ref_type == 'RefSeq_NT':
            # get protein
            refseq_nm = value.split('.')[0]

            if not refseq_nm or not refseq_nm.startswith('NM') or not full_uniprot:
                return

            try:
                protein = Protein.query.filter_by(refseq=refseq_nm).one()
            except NoResultFound:
                return

            try:
                uniprot, isoform = full_uniprot.split('-')
                isoform = int(isoform)
            except ValueError:
                # only one isoform ?
                # print('No isoform specified for', full_uniprot, refseq_nm)
                uniprot = full_uniprot
                isoform = 1

            reference, new = get_or_create(ProteinReferences, protein=protein)
            uniprot_entry, new_uniprot = get_or_create(UniprotEntry, accession=uniprot, isoform=isoform)
            reference.uniprot_entries.append(uniprot_entry)
            references[uniprot].append(reference)

            if new:
                db.session.add(reference)
            if new_uniprot:
                db.session.add(uniprot_entry)

    ensembl_references_to_collect = {
        'Ensembl_PRO': 'peptide_id'
    }

    def add_references_by_uniprot(data):

        full_uniprot, ref_type, value = data

        if '-' in full_uniprot:
            uniprot, isoform = full_uniprot.split('-')
            uniprot_tied_references = references.get(uniprot, None)
            if not uniprot_tied_references:
                return

            relevant_references = []
            # select relevant references:
            for reference in uniprot_tied_references:
                if any(entry.isoform == int(isoform) for entry in reference.uniprot_entries):
                    relevant_references.append(reference)

        else:
            uniprot_tied_references = references.get(full_uniprot, None)
            if not uniprot_tied_references:
                return
            relevant_references = uniprot_tied_references

        if ref_type == 'UniProtKB-ID':
            # http://www.uniprot.org/help/entry_name
            # "Each >reviewed< entry is assigned a unique entry name upon integration into UniProtKB/Swiss-Prot"
            # Entry names comes in format: X_Y;
            # - for Swiss-Prot entry X is a mnemonic protein identification code (at most 5 characters)
            # - for TrEMBL entry X is the same as accession code (6 to 10 characters)
            x, y = value.split('_')

            if len(x) <= 5:
                for reference in relevant_references:
                    assert '-' not in full_uniprot
                    matching_entries = [entry for entry in reference.uniprot_entries if entry.accession == full_uniprot]
                    if len(matching_entries) != 1:
                        print(f'More than one match for reference: {reference}: {matching_entries}')
                    if not matching_entries:
                        print(f'No matching entries for reference: {reference}: {matching_entries}')
                        continue
                    entry = matching_entries[0]
                    entry.reviewed = True

            return

        if ref_type in ensembl_references_to_collect:

            attr = ensembl_references_to_collect[ref_type]

            for relevant_reference in relevant_references:
                attrs = {'reference': relevant_reference, attr: value}

                peptide, new = get_or_create(EnsemblPeptide, **attrs)

                if new:
                    db.session.add(peptide)

    def add_ncbi_mappings(data):
        # 9606    3329    HSPD1   NG_008915.1     NM_199440.1     NP_955472.1     reference standard
        taxonomy, entrez_id, gene_name, refseq_gene, lrg, refseq_nucleotide, t, refseq_peptide, p, category = data

        refseq_nm = refseq_nucleotide.split('.')[0]

        if not refseq_nm or not refseq_nm.startswith('NM'):
            return

        try:
            protein = Protein.query.filter_by(refseq=refseq_nm).one()
        except NoResultFound:
            return

        reference, new = get_or_create(ProteinReferences, protein=protein)

        if new:
            db.session.add(reference)

        reference.refseq_np = refseq_peptide.split('.')[0]
        reference.refseq_ng = refseq_gene.split('.')[0]
        gene = protein.gene

        if gene.name != gene_name:
            print(f'Gene name mismatch for RefSeq mappings: {gene.name} vs {gene_name}')

        entrez_id = int(entrez_id)

        if gene.entrez_id:
            if gene.entrez_id != entrez_id:
                print(f'Entrez ID mismatch for isoforms of {gene.name} gene: {gene.entrez_id}, {entrez_id}')
                if gene.name == gene_name:
                    print(
                        f'Overwriting {gene.entrez_id} entrez id with {entrez_id} for {gene.name} gene, '
                        f'because record with {entrez_id} has matching gene name'
                    )
                    gene.entrez_id = entrez_id
        else:
            gene.entrez_id = entrez_id

    parse_tsv_file(refseq_lrg, add_ncbi_mappings, file_header=[
        '#tax_id', 'GeneID', 'Symbol', 'RSG', 'LRG', 'RNA', 't', 'Protein', 'p', 'Category'
    ])

    # add mappings retrieved from UCSC tables for completeness
    header = ['#name', 'product', 'mrnaAcc', 'protAcc', 'geneName', 'prodName', 'locusLinkId', 'omimId']
    for line in iterate_tsv_gz_file(refseq_link, header):
        gene_name, protein_full_name, refseq_nm, refseq_peptide, _, _, entrez_id, omim_id = line

        if not refseq_nm or not refseq_nm.startswith('NM'):
            continue

        try:
            protein = Protein.query.filter_by(refseq=refseq_nm).one()
        except NoResultFound:
            continue

        gene = protein.gene

        if gene.name != gene_name:
            print(f'Gene name mismatch for RefSeq mappings: {gene.name} vs {gene_name}')

        entrez_id = int(entrez_id)

        if protein_full_name:
            if protein.full_name:
                if protein.full_name != protein_full_name:
                    print(
                        f'Protein full name mismatch: {protein.full_name} vs {protein_full_name} for {protein.refseq}'
                    )
                continue
            protein.full_name = protein_full_name

        if gene.entrez_id:
            if gene.entrez_id != entrez_id:
                print(f'Entrez ID mismatch for isoforms of {gene.name} gene: {gene.entrez_id}, {entrez_id}')
                if gene.name == gene_name:
                    print(
                        f'Overwriting {gene.entrez_id} entrez id with {entrez_id} for {gene.name} gene, '
                        f'because record with {entrez_id} has matching gene name'
                    )
                    gene.entrez_id = entrez_id
        else:
            gene.entrez_id = entrez_id

        if refseq_peptide:
            reference, new = get_or_create(ProteinReferences, protein=protein)

            if new:
                db.session.add(reference)

            if reference.refseq_np and reference.refseq_np != refseq_peptide:
                print(
                    f'Refseq peptide mismatch between LRG and UCSC retrieved data: '
                    f'{reference.refseq_np} vs {refseq_peptide} for {protein.refseq}'
                )

            reference.refseq_np = refseq_peptide

    parse_tsv_file(path, add_uniprot_accession, file_opener=gzip.open, mode='rt')
    parse_tsv_file(path, add_references_by_uniprot, file_opener=gzip.open, mode='rt')

    return [reference for reference_group in references.values() for reference in reference_group]


def select_preferred_isoform(gene):

    if not gene.isoforms:
        return

    def isoform_ordering(isoform):
        """Return a tuple for an isoform which will be used to compare isoforms"""

        # Isoforms which were added earlier to RefSeq should be more familiar to researchers/
        # Also, the "older" (in RefSeq db) isoforms may correspond to isoforms more abundant
        # in cells as those were easier to observed. Here we assume that the isoforms which
        # were added to RefSeq earlier have lower id numbers. It is valid across sequences
        # with common prefixes (like: all sequences with NM_).

        assert isoform.refseq[:3] == 'NM_'

        isoform_age_in_refseq_db = int(isoform.refseq[3:])

        return isoform.is_swissprot_canonical_isoform, isoform.length, -isoform_age_in_refseq_db

    isoforms = sorted(gene.isoforms, key=isoform_ordering, reverse=True)

    return isoforms[0]


# TODO: move after mappings import?
@importer
def select_preferred_isoforms():
    """Perform selection of preferred isoform on all genes in database.

    As preferred isoform the longest isoform will be used. If two isoforms
    have the same length, the isoform with lower refseq identifier will be
    chosen. See implementation details in select_preferred_isoform()
    """
    print('Choosing preferred isoforms:')
    genes = Gene.query.all()

    for gene in tqdm(genes):
        isoform = select_preferred_isoform(gene)
        if isoform:
            gene.preferred_isoform = isoform
        else:
            name = gene.name
            assert not gene.isoforms
            # remove(gene)
            print(f'Gene {name} has no isoforms')


@importer
def conservation(path='data/hg19.100way.phyloP100way.bw', ref_gene_path='data/refGene.txt.gz'):
    from helpers.bioinf import read_genes_data
    from analyses.conservation.scores import scores_for_proteins

    genes_data = read_genes_data(ref_gene_path)

    duplicated_in_ucsc = genes_data[genes_data.index.duplicated()].sort_index()
    print('Duplicated UCSC data:')
    print(duplicated_in_ucsc)

    print('Duplicates summary by chromosome:')
    duplicated_in_ucsc = duplicated_in_ucsc.reset_index()
    print(duplicated_in_ucsc.chrom.value_counts())

    proteins = get_proteins()

    phylo_p_tracks, phylo_details = scores_for_proteins(proteins.values(), genes_data, path)

    del genes_data

    for protein, scores in phylo_p_tracks.items():
        protein.conservation = ';'.join(
            f'{score:.2f}'.strip('0').replace('-0', '-').rstrip('.').rstrip('-')
            for score in scores
        )


@importer
def disorder(path='data/all_RefGene_disorder.fa'):
    # library(seqinr)
    # load("all_RefGene_disorder.fa.rsav")
    # write.fasta(sequences=as.list(fa1_disorder), names=names(fa1_disorder),
    # file.out='all_RefGene_disorder.fa', as.string=T)
    print('Loading disorder data:')
    proteins = get_proteins()

    def on_header(header):
        assert header in proteins
        return header

    def on_sequence(name, line):
        proteins[name].disorder_map += line

    parse_fasta_file(path, on_sequence, on_header)

    for protein in proteins.values():
        if len(protein.disorder_map) == protein.length:
            continue

        warn(
            f'Protein {protein} disorder: {len(protein.disorder_map)} '
            f'does not match the length of protein: {protein.length}'
        )

        if len(protein.disorder_map) > protein.length:
            warn(f'Trimming the disorder track to {protein.length}')
            protein.disorder_map = protein.disorder_map[:protein.length]


@importer
def domains(path='data/biomart_protein_domains_20072016.txt'):
    proteins = get_proteins()

    print('Loading domains:')

    interpro_domains = create_key_model_dict(InterproDomain, 'accession')
    new_domains = []

    skipped = 0
    wrong_length = 0
    not_matching_chrom = []

    header = [
        'Ensembl Gene ID', 'Ensembl Transcript ID', 'Ensembl Protein ID',
        'Chromosome Name', 'Gene Start (bp)', 'Gene End (bp)',
        'RefSeq mRNA [e.g. NM_001195597]', 'Interpro ID',
        'Interpro Short Description', 'Interpro Description',
        'Interpro end', 'Interpro start'
    ]

    def parser(line):

        nonlocal skipped, wrong_length, not_matching_chrom

        try:
            protein = proteins[line[6]]  # by refseq
        except KeyError:
            skipped += 1
            return

        # If there is no data about the domains, skip this record
        if len(line) == 7:
            return

        try:
            assert len(line) == 12
        except AssertionError:
            print(line, len(line))

        # does start is lower than end?
        assert int(line[11]) < int(line[10])

        accession = line[7]

        # according to:
        # http://www.ncbi.nlm.nih.gov/pmc/articles/PMC29841/#__sec2title
        assert accession.startswith('IPR')

        start, end = int(line[11]), int(line[10])

        # TODO: the assertion fails for some domains: what to do?
        # assert end <= protein.length
        if end > protein.length:
            wrong_length += 1

        if line[3] != protein.gene.chrom:
            skipped += 1
            not_matching_chrom.append(line)
            return

        if accession not in interpro_domains:

            interpro = InterproDomain(
                accession=line[7],   # Interpro Accession
                short_description=line[8],   # Interpro Short Description
                description=line[9],   # Interpro Description
            )

            interpro_domains[accession] = interpro

        interpro = interpro_domains[accession]

        similar_domains = [
            # select similar domain occurrences with criteria being:
            domain for domain in protein.domains
            # - the same interpro id
            if domain.interpro == interpro and
            # - at least 75% of common coverage for shorter occurrence of domain
            (
                (min(domain.end, end) - max(domain.start, start))
                / min(len(domain), end - start)
                > 0.75
            )
        ]

        if similar_domains:
            try:
                assert len(similar_domains) == 1
            except AssertionError:
                print(similar_domains)
            domain = similar_domains[0]

            domain.start = min(domain.start, start)
            domain.end = max(domain.end, end)
        else:

            domain = Domain(
                interpro=interpro,
                protein=protein,
                start=start,
                end=end
            )
            new_domains.append(domain)

    parse_tsv_file(path, parser, header)

    print(
        'Domains loaded,', skipped, 'proteins skipped.',
        'Domains exceeding proteins length:', wrong_length,
        'Domains skipped due to not matching chromosomes:',
        len(not_matching_chrom)
    )
    return new_domains


@importer
def domains_hierarchy(path='data/ParentChildTreeFile.txt'):
    """Add domains hierarchy basing on InterPro tree file.

    Domains (precisely: instances of InterproDomain model) which
    already exist in the database will be updated with 'parent'
    (reference to the domain immediate above in hierarchy, None
    if the domain is top-level) and 'level' (how deep in hierarchy
    the domain lies?) properties.

    If a domain is not in database, it will be created and added.

    Existing domains are looked-up in database using InterPro id
    If the domain retrieved using interpro accession has different
    description in tree file than in database, it will be reported.
    """
    from re import compile

    print('Loading InterPro hierarchy:')

    expr = compile('^(?P<dashes>-*)(?P<interpro_id>\w*)::(?P<desc>.*?)::$')

    previous_level = 0
    domains_stack = []
    new_domains = []

    def parser(line):
        nonlocal previous_level, new_domains, domains_stack

        result = expr.match(line)

        dashes = result.group('dashes')
        interpro_id = result.group('interpro_id')
        description = result.group('desc')

        # at each level deeper two dashes are added, starting from 0
        level = len(dashes) // 2

        assert len(dashes) % 2 == 0

        # look out for "jumps" - we do not expect those
        assert level - previous_level <= 1 or level == 0

        domain, created = get_or_create(InterproDomain, accession=interpro_id)
        if created:
            domain.description = description
            new_domains.append(domain)

        # we are on the top level: no parents here
        if level == 0:
            domains_stack = [domain]
            parent = None
        else:
            # we need to go a level deeper
            if level > len(domains_stack) - 1:
                parent = domains_stack[-1]
                domains_stack.append(domain)
            # we either are on the same level or jump up in hierarchy
            else:
                # remove leaf
                domains_stack.pop()

                # go up in hierarchy if needed
                while level != len(domains_stack):
                    domains_stack.pop()

                assert level == len(domains_stack)
                parent = domains_stack[-1]
                domains_stack.append(domain)

        if domain.description != description:
            print(
                'InterproDomain descriptions differs between database and',
                'hierarchy file: "{0}" vs "{1}" for: {2}'.format(
                    domain.description,
                    description,
                    interpro_id
                ))

        domain.level = level
        domain.parent = parent

        previous_level = level

    parse_text_file(path, parser)

    print('Domains\' hierarchy built,', len(new_domains), 'new domains added.')
    return new_domains


@importer
def domains_types(path='data/interpro.xml.gz'):
    from xml.etree import ElementTree
    import gzip

    print('Loading extended InterPro annotations:')

    domains = create_key_model_dict(InterproDomain, 'accession')

    with gzip.open(path) as interpro_file:
        tree = ElementTree.parse(interpro_file)

    entries = tree.getroot().findall('interpro')

    for entry in tqdm(entries):
        try:
            domain = domains[entry.get('id')]
        except KeyError:
            continue
        domain.type = entry.get('type')


@importer
def cancers(path='data/cancer_types.txt'):
    print('Loading cancer data:')

    cancers = []

    def parser(line):
        code, name, color = line
        cancer, created = get_or_create(Cancer, name=name)
        if created:
            cancers.append(cancer)

        cancer.code = code

    parse_tsv_file(path, parser)

    return cancers


def get_preferred_gene_isoform(gene_name):
    """Return a preferred isoform (protein) for a gene of given name.

    If there is a gene, it has a preferred isoform. Implemented as
    database query to avoid keeping all genes in memory - should be
    still feasible as there are not so many genes as proteins."""
    from sqlalchemy.orm.exc import NoResultFound

    # TODO consider same trick as for proteins: cache in mutable func arg

    try:
        gene = Gene.query.filter(Gene.name.ilike(gene_name)).one()
    except NoResultFound:
        return None
    return gene.preferred_isoform


@importer
def kinase_mappings(path='data/curated_kinase_IDs.txt'):
    """Create kinases from `kinase_name gene_name` mappings.

    For each kinase a `preferred isoforms` of given gene will be used.

    If given kinase already is in the database and has an isoform
    associated, the association will be superseded with the new one.

    Returns:
        list of created isoforms
    """
    known_kinases = create_key_model_dict(Kinase, 'name')

    new_kinases = []

    def parser(line):
        kinase_name, gene_name = line
        protein = get_preferred_gene_isoform(gene_name)

        if not protein:
            print(
                'No isoform for %s kinase mapped to %s gene!' %
                (kinase_name, gene_name)
            )
            return

        if kinase_name in known_kinases:
            kinase = known_kinases[kinase_name]
            if kinase.protein and kinase.protein != protein:

                print(
                    'Overriding kinase-protein association for '
                    '%s kinase. Old isoform: %s; new isoform: %s.'
                    % (
                        kinase_name,
                        kinase.protein.refseq,
                        protein.refseq
                    )
                )
            kinase.protein = protein

        else:
            new_kinases.append(
                Kinase(name=kinase_name, protein=protein)
            )

    parse_tsv_file(path, parser)

    return new_kinases


@importer
def kinase_classification(path='data/regphos_kinome_scraped_clean.txt'):

    known_kinases = create_key_model_dict(Kinase, 'name', True)
    known_groups = create_key_model_dict(KinaseGroup, 'name', True)

    new_groups = []

    print('Loading protein kinase groups:')

    header = [
        'No.', 'Kinase', 'Group', 'Family', 'Subfamily', 'Gene.Symbol',
        'gene.clean', 'Description', 'group.clean'
    ]

    def parser(line):

        # note that the subfamily is often absent
        group, family, subfamily = line[2:5]

        # the 'gene.clean' [6] fits better to the names
        # of kinases used in all other data files
        kinase_name = line[6]

        # 'group.clean' is not atomic and is redundant with respect to
        # family and subfamily. This check assures that in case of a change
        # the maintainer would be able to spot the inconsistency easily
        clean = family + '_' + subfamily if subfamily else family
        assert line[8] == clean

        if kinase_name.lower() not in known_kinases:
            kinase = Kinase(
                name=kinase_name,
                protein=get_preferred_gene_isoform(kinase_name)
            )
            known_kinases[kinase_name.lower()] = kinase

        # the 'family' corresponds to 'group' in the all other files
        if family.lower() not in known_groups:
            group = KinaseGroup(
                name=family
            )
            known_groups[family.lower()] = group
            new_groups.append(group)

        known_groups[family.lower()].kinases.append(known_kinases[kinase_name.lower()])

    parse_tsv_file(path, parser, header)

    return new_groups


@importer
def clean_from_wrong_proteins(soft=True):
    """Removes proteins with premature or lacking stop codon.

    Args:
        soft: use if the faulty proteins were not committed yet (expunge instead of delete)

    """
    print('Removing proteins with misplaced stop codons:')
    from database.manage import remove

    proteins = get_proteins()

    stop_inside = 0
    lack_of_stop = 0
    no_stop_at_the_end = 0

    to_remove = set()

    for protein in tqdm(proteins.values()):
        hit = False
        if '*' in protein.sequence[:-1]:
            stop_inside += 1
            hit = True
        if protein.sequence[-1] != '*':
            no_stop_at_the_end += 1
            hit = True
        if '*' not in protein.sequence:
            lack_of_stop += 1
            hit = True
        if hit:
            to_remove.add(protein)

    with db.session.no_autoflush:
        removed = set()
        for protein in to_remove:
            removed.add(protein.refseq)

            gene = protein.gene
            gene.preferred_isoform = None

            remove(protein, soft)

            # remove object
            del proteins[protein.refseq]

    select_preferred_isoforms()

    print('Removed proteins of sequences:')
    print('\twith stop codon inside (excluding the last pos.):', stop_inside)
    print('\twithout stop codon at the end:', no_stop_at_the_end)
    print('\twithout stop codon at all:', lack_of_stop)


@importer
def calculate_interactors():
    print('Precalculating interactors counts:')

    proteins = get_proteins()

    for protein in tqdm(proteins.values()):
        protein.interactors_count = protein._calc_interactors_count()


ListData = namedtuple('ListData', 'name path mutations_source')


@importer
def active_driver_gene_lists(
        lists=(
            ListData(
                name='Cancer (TCGA PanCancerAtlas)',
                path='data/mc3.activedriver.2017-11-28.txt',
                mutations_source=MC3Mutation
            ),
            ListData(
                name='Clinical (ClinVar)',
                path='data/ActiveDriver1_result_pvalue_less_0.01_InheritedMutation-2017-02-16.txt',
                mutations_source=InheritedMutation
            )
        ),
        fdr_cutoff=0.01
):
    current_gene_lists = [
        existing_list.name
        for existing_list in GeneList.query.all()
    ]
    gene_lists = []

    for list_data in lists:
        if list_data.name in current_gene_lists:
            print(
                'Skipping gene list %s: already present in database' %
                list_data.name
            )
            continue

        gene_list = GeneList(
            name=list_data.name,
            mutation_source_name=(
                list_data.mutations_source.name
                if list_data.mutations_source
                else None
            )
        )

        header = ['gene', 'p', 'fdr']

        to_high_fdr_count = 0
        list_entries = []

        def parser(line):
            gene_name, p_value, fdr = line
            p_value = float(p_value)
            fdr = float(fdr)

            nonlocal to_high_fdr_count

            if fdr >= fdr_cutoff:
                to_high_fdr_count += 1
                return

            gene, created = get_or_create(Gene, name=gene_name)

            entry = GeneListEntry(
                gene=gene,
                p=p_value,
                fdr=fdr
            )
            list_entries.append(entry)

        parse_tsv_file(list_data.path, parser, header)

        gene_list.entries = list_entries

        gene_lists.append(gene_list)

    return gene_lists


@importer
def full_gene_names(path='data/Homo_sapiens.gene_info.gz'):
    expected_header = [
        '#tax_id', 'GeneID', 'Symbol', 'LocusTag', 'Synonyms', 'dbXrefs', 'chromosome', 'map_location',
        'description', 'type_of_gene', 'Symbol_from_nomenclature_authority', 'Full_name_from_nomenclature_authority',
        'Nomenclature_status', 'Other_designations', 'Modification_date', 'Feature_type'
    ]

    known_genes = {gene.entrez_id: gene for gene in Gene.query.all()}
    genes_covered = set()

    for line_data in iterate_tsv_gz_file(path, expected_header):

        full_name = line_data[11]

        if full_name == '-':
            continue

        entrez_id = int(line_data[1])

        if entrez_id not in known_genes:
            continue

        gene = known_genes[entrez_id]
        gene.full_name = full_name

        genes_covered.add(gene)

    covered = len(genes_covered)
    count = Gene.query.count()
    print(
        'Imported full gene names for %s genes (%s percent of %s total in database, %s percent of those %s having Entrez ID)' %
        (covered, covered * 100 // count, count, covered * 100//len(known_genes), len(known_genes))
    )


@importer
def pathways(path='data/hsapiens.pathways.NAME.gmt'):
    """Loads pathways from given '.gmt' file.

    New genes may be created and should automatically be added
    to the session with pathways as those have a relationship.
    """
    known_genes = create_key_model_dict(Gene, 'name', lowercase=True)

    pathways = []
    new_genes = []

    def parser(data):
        """Parse GTM file with pathway descriptions.

        Args:
            data: a list of subsequent columns from a single line of GTM file

                For example::

                    ['CORUM:5419', 'HTR1A-GPR26 complex', 'GPR26', 'HTR1A']

        """
        gene_set_name = data[0]
        # Entry description can by empty
        entry_description = data[1].strip()

        entry_gene_names = [
            name.strip()
            for name in data[2:]
        ]

        pathway_genes = []

        for gene_name in entry_gene_names:
            name_lower = gene_name.lower()
            if name_lower in known_genes:
                gene = known_genes[name_lower]
            else:
                gene = Gene(name=gene_name)
                known_genes[name_lower] = gene
                new_genes.append(gene)

            pathway_genes.append(gene)

        pathway = Pathway(
            description=entry_description,
            genes=pathway_genes
        )

        if gene_set_name.startswith('GO'):
            pathway.gene_ontology = int(gene_set_name[3:])
        elif gene_set_name.startswith('REAC'):
            pathway.reactome = int(gene_set_name[5:])
        else:
            raise Exception(
                'Unknown gene set name: "%s"' % gene_set_name
            )

    parse_tsv_file(path, parser)

    print(len(new_genes), 'new genes created')

    return pathways


@importer
def precompute_ptm_mutations():
    print('Counting mutations...')
    total = Mutation.query.filter_by(is_confirmed=True).count()
    mismatch = 0
    for mutation in tqdm(Mutation.query.filter_by(is_confirmed=True), total=total):
        pos = mutation.position
        is_ptm_related = mutation.protein.has_sites_in_range(pos - 7, pos + 7)
        if is_ptm_related != mutation.precomputed_is_ptm:
            mismatch += 1
            mutation.precomputed_is_ptm = is_ptm_related
    print('Precomputed values of %s mutations has been computed and updated' % mismatch)
    return []


@importer
def drugbank(path='data/drugbank/drugbank.tsv'):

    drugs = set()

    # in case we need to query drugbank, it's better to keep names compat.
    drug_type_map = {
        'BiotechDrug': 'biotech',
        'SmallMoleculeDrug': 'small molecule'
    }

    def parser(data):
        drug_id, gene_name, drug_name, drug_groups, drug_type_name = data
        target_gene = Gene.query.filter_by(name=gene_name).first()

        if target_gene:
            drug, created = get_or_create(Drug, name=drug_name)
            if created:
                drugs.add(drug)
            drug.target_genes.append(target_gene)
            drug.drug_bank_id = drug_id

            for drug_group_name in drug_groups.split(';'):
                if drug_group_name != 'NA':
                    drug_group, created = get_or_create(DrugGroup, name=drug_group_name)
                    drug.groups.add(drug_group)

            if drug_type_name != 'NA':
                drug_type, created = get_or_create(DrugType, name=drug_type_map[drug_type_name])
                drug.type = drug_type

    # TODO: the header has type and group swapped
    header = 'DRUG_id	GENE_symbol	DRUG_name	DRUG_type	DRUG_group'.split('\t')

    parse_tsv_file(path, parser, header)

    return drugs


@importer
def sites_motifs(data=None):

    motifs_data = [
        # site_type_name, name, pattern (Python regular expression), sequences for pseudo logo

        # https://prosite.expasy.org/PDOC00001
        [
            'N-glycosylation', 'N-linked', '.{7}N[^P][ST].{5}',
            [
                ' ' * 7 + f'N{aa}{st}' + ' ' * 5
                for aa in aa_symbols if aa != 'P'
                for st in 'ST'
            ]
        ],
        # https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4721579/
        [
            'N-glycosylation', 'N-linked - atypical', '.{7}N[^P][CV].{5}',
            [
                ' ' * 7 + f'N{aa}{cv}' + ' ' * 5
                for aa in aa_symbols if aa != 'P'
                for cv in 'CV'
            ]

        ],

        # Based on https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1301293/
        ['O-glycosylation', 'O-linked TAPP', '.{7}TAPP', [' ' * 7 + 'TAPP' + ' ' * 5]],
        ['O-glycosylation', 'O-linked TSAP', '.{7}TSAP', [' ' * 7 + 'TSAP' + ' ' * 5]],
        ['O-glycosylation', 'O-linked TV.P', '.{7}TV.P', [' ' * 7 + 'TV.P' + ' ' * 5]],
        [
            'O-glycosylation', 'O-linked [ST]P.P', '.{7}[ST]P.P',
            [
                ' ' * 7 + f'{st}P P' + ' ' * 5
                for st in 'ST'
            ]
        ],

        # https://www.uniprot.org/help/carbohyd
        [
            'C-glycosylation', 'C-linked W..W', '.{7}W..W.{4}',
            [' ' * 7 + 'W  W' + ' ' * 4]
        ],
        [
            'C-glycosylation', 'C-linked W..W', '.{4}W..W.{7}',
            [' ' * 4 + 'W  W' + ' ' * 7]
        ],
        [
            'C-glycosylation', 'C-linked W[ST].C', '.{7}W[ST].C.{4}',
            [
                ' ' * 7 + f'W{st} C' + ' ' * 4
                for st in 'ST'
            ]
        ],

    ]

    if data:
        motifs_data = data

    new_motifs = []

    for site_type_name, name, pattern, sequences in motifs_data:
        site_type, _ = get_or_create(SiteType, name=site_type_name)
        motif, new = get_or_create(SiteMotif, name=name, pattern=pattern, site_type=site_type)

        if new:
            new_motifs.append(motif)
            db.session.add(motif)

        motif.generate_pseudo_logo(sequences)

    return new_motifs
