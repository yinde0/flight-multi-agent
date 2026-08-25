# Dataset Provenance Plan

The checked-in fixtures are synthetic and contain no real traveler data. External datasets should be imported into a separate, versioned data-build process rather than copied directly into the golden directory.

## Candidate public sources

- US Bureau of Transportation Statistics On-Time Performance for scheduled and actual times, delay minutes, cancellations, diversions, and reported delay causes: <https://transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ>
- NOAA Integrated Surface Database for historical airport weather observations: <https://www.ncei.noaa.gov/products/land-based-station/integrated-surface-database>
- NOAA NOMADS for archived forecast-model runs: <https://www.emc.ncep.noaa.gov/emc/pages/nomads.php>
- OpenSky scientific datasets for aircraft movement evidence, not commercial delay or cancellation truth: <https://opensky-network.org/data/>
- OurAirports public-domain airport metadata: <https://ourairports.com/data/>
- Viva Mais synthetic travel-document images and extraction labels, Apache-2.0: <https://huggingface.co/datasets/marinarosa/vivamais-synthetic-ptbr>

## Provenance record

Every imported source snapshot should record:

- Source URL and owner.
- Retrieval timestamp.
- Source release/version or date range.
- License and redistribution constraints.
- Original checksum.
- Transformation code revision.
- Filtering and join logic.
- Human-labeling revision.

Historical final outcomes do not contain provider update timelines. Those timelines must be recorded prospectively under provider terms or synthesized from final outcomes and explicitly marked synthetic.
