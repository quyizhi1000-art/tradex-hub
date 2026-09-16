# Industry catalog collection

The analysis worker owns the daily refresh and the existing taxonomy service is
the sole catalog writer. The Web selection views read the accepted local revision;
they never fetch a provider or alter archived strategy candidates.

## Classification

- Statistical industry: SW2021 paths. Fetch all 31 L1 partitions; select the latest
  effective entry when upstream retains older current rows. Equal-date conflicting
  paths reject the refresh.
- Selection display: THS standard market industry blocks (`881xxx.TI`, type `I`).
  The broader THS index endpoint also contains `884` subdivisions and other
  classifications; these are excluded. Examples: SAIC = 汽车整车, Hongyuan = 军工电子.
- These are independent profile fields. Updating a display block never overwrites
  statistical paths, company-business evidence, or historical strategy evidence.

## Completeness and failure handling

Each collection is reconciled against the current listed-stock master. Query
remaining missing stocks individually (at most 64); a larger gap rejects the
refresh without launching a per-stock full-market crawl. Failed calls propagate.
Unresolved THS multiple-block memberships remain unclassified, with explicit
conflict flags, instead of depending on response order.

The routed quality gate requires unique stock memberships, valid complete paths,
current dates, an exact missing-instrument list, and at least 99% coverage. A
missing entry is only accepted after individual source verification. Publication
also rejects a stock-master count drop greater than 1%, or loss of an existing
industry mapping for any retained stock. Failure preserves the prior catalog.

Validated catalogs carry `industry_coverage_checked:v1` and
`market_industry_coverage_checked:v1`. A same-day catalog lacking either marker is
eligible for repair under the worker's existing single-flight and 30-minute retry
throttle. Normal refresh remains daily; automatic selection generation is unchanged.

`InstrumentTaxonomyService.refresh_market_industries` can attach freshly validated
market blocks to today's accepted catalog without refetching financial disclosures.
It requires the same stock universe and checks the catalog revision before writing.
The complete daily refresh acquires both classifications through the same gateway.
