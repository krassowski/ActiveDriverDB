from flask import request
from flask import jsonify
from flask import redirect
from flask import url_for
from flask import render_template as template
from flask_classful import FlaskView
from models import Protein
from models import Mutation
from website.helpers.tracks import Track
from website.helpers.tracks import TrackElement
from website.helpers.tracks import PositionTrack
from website.helpers.tracks import SequenceTrack
from website.helpers.tracks import MutationsTrack
from website.helpers.filters import FilterSet
from website.helpers.filters import Filters
from website.helpers.filters import Filter


def get_source_field(filters):
    source = filters.sources
    source_field_name = Mutation.source_fields[source]
    return source_field_name


class ProteinView(FlaskView):
    """Single protein view: includes needleplot and sequence"""

    allowed_filters = FilterSet([
        Filter('sources', 'in', 'TCGA', 'select', 'Source',
               choices=list(Mutation.source_fields.keys())),
        Filter('is_ptm', 'eq', None, 'with_without', 'PTM mutations'),
    ])

    def index(self):
        """Show SearchView as deafault page"""
        return redirect(url_for('SearchView:index', target='proteins'))

    def show(self, refseq):
        """Show a protein by:

        + needleplot
        + tracks (seuqence + data tracks)
        """
        active_filters = FilterSet.from_request(request)

        protein = Protein.query.filter_by(refseq=refseq).first_or_404()

        disorder = [
            TrackElement(*region) for region in protein.disorder_regions
        ]
        mutations = active_filters.filtered(protein.mutations)

        tracks = [
            PositionTrack(protein.length, 25),
            SequenceTrack(protein),
            Track('disorder', disorder),
            Track(
                'domains',
                [
                    TrackElement(
                        domain.start,
                        domain.end - domain.start,
                        domain.interpro.accession,
                        domain.interpro.description
                    )
                    for domain in protein.domains
                ]
            ),
            MutationsTrack(mutations)
        ]

        filters = Filters(active_filters, self.allowed_filters)

        if filters.active.sources in ('TCGA', 'ClinVar'):
            val_type = 'Count'
        else:
            val_type = 'Frequency'

        # repeated on purpose
        mutations = active_filters.filtered(protein.mutations)

        return template(
            'protein.html', protein=protein, tracks=tracks,
            filters=filters, mutations=mutations, value_type=val_type,
            log_scale=(val_type == 'Frequency')
        )

    def mutations(self, refseq):
        """List of mutations suitable for needleplot library"""
        protein = Protein.query.filter_by(refseq=refseq).first_or_404()
        filters = request.args.get('filters', '')
        filters = FilterSet.from_string(filters)

        response = []

        mutations = list(filter(filters.test, protein.mutations))

        source = filters.sources
        source_field_name = get_source_field(filters)

        def get_metadata(mutation):
            nonlocal source_field_name, source
            meta = {}
            source_specific_data = getattr(mutation, source_field_name)
            if isinstance(source_specific_data, list):
                meta[source] = [
                    datum.representation
                    for datum in source_specific_data
                ]
                meta[source].sort(key=lambda rep: rep['Value'], reverse=True)
            else:
                meta[source] = source_specific_data.representation
            mimp = getattr(mutation, 'meta_MIMP')
            if mimp:
                meta['MIMP'] = mimp.representation
            return meta

        def get_value(mutation):
            nonlocal source_field_name

            meta = getattr(mutation, source_field_name)
            if isinstance(meta, list):
                return sum((data.value for data in meta))
            return meta.value

        for mutation in mutations:
            needle = {
                'coord': mutation.position,
                'value': get_value(mutation),
                'category': mutation.impact_on_ptm,
                'alt': mutation.alt,
                'ref': mutation.ref,
                'meta': get_metadata(mutation),
                'sites': [
                    site.representation
                    for site in mutation.find_closest_sites()
                ]
            }
            response += [needle]

        return jsonify(response)

    def sites(self, refseq):
        """List of sites suitable for needleplot library"""

        protein = Protein.query.filter_by(refseq=refseq).first_or_404()

        response = [
            {
                'start': site.position - 7,
                'end': site.position + 7,
                'type': str(site.type)
            } for site in protein.sites
        ]

        return jsonify(response)
