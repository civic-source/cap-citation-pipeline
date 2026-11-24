# Observed CourtListener bulk contract

Qualification date: 2026-11-24. This records a publication observation, not a
promise about later exports. The retained inventory contains all 1,111 objects
from two bounded S3 ListObjectsV2 pages; the second page is not truncated.

## Native source and joins

Latest shared generation observed: 2026-09-30.

| Table | Compressed bytes |
|---|---:|
| courts | 81,226 |
| dockets | 5,144,011,100 |
| opinion-clusters | 2,467,613,433 |
| opinions | 55,252,643,429 |
| citations (optional reporter display) | 127,468,518 |

The four core tables total 62,864,349,188 compressed bytes. Reporter citations
bring this to 62,991,817,706 bytes. The citation map is case-to-case, not a USC
reference index. Retained inventory metadata records complete keys, object sizes,
ETags and publisher modification times. Multipart ETags are not content hashes;
no VersionId or SHA-256 response header was exposed by the observed endpoints.

The published 22-column opinions CSV joins as follows:

`opinions.cluster_id → opinion-clusters.id → opinion-clusters.docket_id → dockets.id → dockets.court_id → courts.id`

Clusters provide case/date/status metadata; court jurisdiction supplies scope.
The observed courts table uses `F` for federal appellate/Supreme Court and `FD`
for federal district courts. Tables are independently exported from a live
source; foreign-key consistency cannot be assumed solely from matching dates.

The observed schema/exporter defines `search_opinioncontent` and the schema has
`search_opinion.main_version_id`. No opinion-contents object was present anywhere
in the complete public inventory, and the published legacy opinions CSV omits
`main_version_id`. A large opinions table therefore does not establish complete
current text coverage. Missing joins and unavailable text must remain unresolved.
The observed schema was 555,339 bytes with SHA-256
`c43db7879ecb64a79089db5a12467c4f218b195064e787b943878be947aa061f`;
it was inspected as data and never executed.

## Parser and resource contract

PostgreSQL COPY CSV uses UTF-8 headers, quoted multiline records and backslash
escaping. A candidate Python reader uses `escapechar='\\'`, `doublequote=False`
and `strict=True`. Unquoted empty (null) and quoted empty (blank) differ upstream;
a parser that loses that distinction needs retained raw records for audit.
Physical-line splitting is invalid. Evidence offsets must distinguish raw CSV
bytes from decoded/normalized opinion text.

Bounded `Range: bytes=0-262143`/`If-Match` probes returned HTTP 206. Courts was
fully retained; the other probes were prefixes. At a 2 MiB decompression ceiling,
they yielded 58 opinions, 230 clusters, 2,146 dockets and 8,015 reporter citations.
There were zero complete opinion/cluster/docket joins and zero observed Title 18
hits in that prefix. These samples qualify parser inputs, not representative
corpus coverage. Arbitrary bzip2 ranges are not independent row filters.

Uncompressed full-table size is **unmeasured**: prefix compression ratios cannot
provide a defensible complete-corpus estimate. A full job must measure streaming
expansion, selected outputs, simultaneous generations, archive/readback and
isolated restore headroom before choosing its budget. This pilot's 16 GiB cache,
32 MiB source cap and 256 MiB archive cap deliberately refuse the native corpus.
Native joins and multipart archives are tracked in
civic-source/cap-citation-pipeline#17.

## Historical joined alternative and updates

The selected official COLD JSONL sample is a pinned 2023 file, not the entire
2024 Parquet dataset or a current native export. Its fields include `court_type`
(`F`, `FD`, `SA`, etc.), human-label `court_jurisdiction`, nested `opinion_id` and
`opinion_text`; it does not export `court_id`. Optional or absent metadata remains
explicit. See [the operator guide](precedent-bulk.md) for exact source identity,
cache, filing-date filtering and recovery.

The original 2024 COLD publication has 32 Parquet shards totaling 32,204,966,721
bytes at revision `5d8d0d8457ef63b6463af9737da21d3badd924ad`. Its dataset viewer
reports partial conversion; the prior viewer count is not the full dataset size.
No Parquet shard was downloaded by this qualification.

Native bulk publications are quarterly complete snapshots, not deltas. Default
ordinary REST quotas are concurrent minute/hour/day limits; observed official
documentation lists 5/minute, 50/hour and 125/day. An authenticated account's
api-usage response must determine actual budgets/reset times. One unauthenticated
cluster request returned HTTP 401; it was not repeatedly retried. Any incremental
REST implementation must verify supported modified-date filters, stable ordering,
tie-breakers and bounded next links before promising an update cursor. A 0.5-second
request delay alone cannot satisfy all these budgets.

## Primary sources

- [CourtListener bulk documentation](https://wiki.free.law/c/courtlistener/help/api/bulk-data/bulk-legal-data)
- [Public object listing](https://com-courtlistener-storage.s3.us-west-2.amazonaws.com/?list-type=2&prefix=bulk-data%2F&max-keys=1000)
- [Exporter source](https://github.com/freelawproject/courtlistener/blob/main/scripts/make_bulk_data.sh)
- [Account API usage](https://wiki.free.law/c/courtlistener/help/api/rest/v4/api-usage)
- [Query refinement](https://wiki.free.law/c/courtlistener/help/api/rest/v4/query-refinement)
- [Pinned original COLD publication](https://huggingface.co/datasets/harvard-lil/cold-cases/tree/5d8d0d8457ef63b6463af9737da21d3badd924ad)
- [Pinned official JSONL sample](https://raw.githubusercontent.com/harvard-lil/cold-cases-export/5907c8453ffdfb5a6798d4fb8cb8faebdfc1025f/sample.jsonl)
