from collections import defaultdict
from warnings import warn

from tqdm import tqdm

from helpers.plots import bar_plot
from models import Plot, Site, Protein, DataError
from stats.plots import (
    ptm_variability, proteins_variability, most_mutated_sites, active_driver, ptm_mutations,
    gene_ontology, motifs, mimp, enrichment
)
from stats.plots.common import site_types_names
from ..store import cases, CountStore
from ..store.objects import StoreObject


def prefixed_namespace(namespace, prefix=None):
    if not prefix:
        prefix = namespace.__name__.split('.')[-1]

    return {
        f'{prefix}_{key}': value
        for key, value in namespace.__dict__.items()
    }


def objects_to_store_from_module(module):
    return {
        key: value.as_staticmethod()
        for key, value in prefixed_namespace(module).items()
        if isinstance(value, StoreObject)
    }


plot_modules = [
    ptm_variability,
    proteins_variability,
    most_mutated_sites,
    active_driver,
    ptm_mutations,
    gene_ontology,
    motifs,
    mimp,
    enrichment,
]


class Plots(CountStore):

    storage_model = Plot

    for module in plot_modules:
        vars().update(objects_to_store_from_module(module))

    @cases(only_preferred=[True, False])
    @bar_plot
    def sites_in_disordered_regions(self, only_preferred):
        disordered = defaultdict(int)
        not_disordered = defaultdict(int)

        query = Site.query
        if only_preferred:
            query = query.join(Protein).filter(Protein.is_preferred_isoform)

        for site in tqdm(query.all()):
            for site_type in site.types:
                try:
                    if site.in_disordered_region:
                        disordered[site_type.name] += 1
                    else:
                        not_disordered[site_type.name] += 1
                except DataError as e:
                    warn(str(e))

        values = [
            100 * disordered[type_name] / (disordered[type_name] + not_disordered[type_name])
            for type_name in site_types_names
        ]

        return site_types_names, values

    @cases(only_preferred=[True, False])
    @bar_plot
    def sites_counts(self, only_preferred):
        counts = defaultdict(int)

        query = Site.query
        if only_preferred:
            query = query.join(Protein).filter(Protein.is_preferred_isoform)

        for site in tqdm(query.all()):
            for site_type in site.types:
                counts[site_type.name] += 1

        return site_types_names, [counts[type_name] for type_name in site_types_names]
