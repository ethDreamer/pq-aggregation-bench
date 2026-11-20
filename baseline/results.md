# Measured PQ devnet-0 committee geometry

Apple M3 Max (16 logical CPUs, 64 GiB), native macOS ARM64; pinned leanVM `283686575c4e81790591d2c6e3ed21c69c0e3bbe`, experimental prover, rate 2, 16 prover threads. Timings are medians of five warmed requests, or ten for the two largest cases (pooled original and repeat run). Raw samples retain ranges and initial timings. Distinct one-leaf benchmark keys retain the full two-layer XMSS verification work. Each slot has agreeing votes, full participation, and no backlog.

| Active validators | Committee proofs/slot | Signatures/proof | Signatures/slot | Merge jobs | Prove one (ms) | Merge (ms) | Verify final (ms) | Final proof (KiB) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 64 | 1 | 2 | 2 | 0 | 32.8 | 0 | 7.2 | 163.8 |
| 1,024 | 1 | 32 | 32 | 0 | 60.2 | 0 | 7.5 | 155.7 |
| 4,096 | 1 | 128 | 128 | 0 | 114.7 | 0 | 7.7 | 171.2 |
| 8,192 | 2 | 128 | 256 | 1 | 115.3 | 160.5 | 11.7 | 186.5 |
| 16,384 | 4 | 128 | 512 | 1 | 115.1 | 301.8 | 12.3 | 191.9 |
| 32,768 | 8 | 128 | 1,024 | 1 | 130.9 | 647.9 | 13.7 | 194.6 |
| 65,536 | 16 | 128 | 2,048 | 1 | 138.3 | 1,239.5 | 14.4 | 202.0 |
| 131,072 | 16 | 256 | 4,096 | 1 | 236.2 | 1,460.4 | 15.6 | 215.2 |
| 262,144 | 16 | 512 | 8,192 | 1 | 500.3 | 1,573.1 | 17.1 | 215.5 |
| 524,288 | 16 | 1,024 | 16,384 | 1 | 1,161.8 | 1,936.0 | 23.5 | 214.8 |
| 1,048,576 | 16 | 2,048 | 32,768 | 1 | 1,910.0 | 1,730.9 | 20.6 | 213.9 |

Completed 11/11 requested rows. Blank/missing rows have not been measured.

Prove one measures one committee on the machine; it is not the sum of all committee jobs or a measurement of concurrent committee proving. Merge measures one recursive aggregation of the selected child proofs. One-committee cases reuse the proof and have zero merge jobs. Timings include local request serialization and Unix-socket overhead, exclude signature/key generation, network propagation and BN/VC state work. Every proof and complete merged claim union was checked. Limited-use key generation is not production keygen or signing performance.

The two largest cases were repeated because their initial merge timings were inverted. Both complete series are preserved and pooled, rather than selecting the faster run. These shared-machine measurements varied noticeably between runs; the nonmonotonic medians are not evidence that increasing committee size makes merging faster.

## Observed repeat ranges (milliseconds)

| Active validators | Committee prove min–max | Merge min–max |
| ---: | ---: | ---: |
| 64 | 32.1–43.1 | 0 (reuse) |
| 1,024 | 59.1–72.2 | 0 (reuse) |
| 4,096 | 113.2–135.0 | 0 (reuse) |
| 8,192 | 113.8–129.2 | 159.7–225.9 |
| 16,384 | 114.3–191.1 | 290.2–362.3 |
| 32,768 | 124.9–218.3 | 567.6–706.6 |
| 65,536 | 129.3–188.7 | 1218.9–1329.4 |
| 131,072 | 227.9–311.9 | 1398.3–1513.5 |
| 262,144 | 477.7–559.2 | 1516.2–1619.3 |
| 524,288 | 915.0–1625.7 | 1519.3–2456.8 |
| 1,048,576 | 1685.4–2063.9 | 1476.2–1840.2 |

Fixtures and source: [README](../README.md). Individual samples, timestamps, input hashes, proof sizes and host load: [results](results/). This is a local cryptographic benchmark; no percentile or mainnet end-to-end performance claim is made.
