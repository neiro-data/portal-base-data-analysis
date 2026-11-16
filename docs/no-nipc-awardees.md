# Awardee entries without an NIPC

Report on the adjudicatário entries that `sql/ingest/30_rebuild_bridge.sql` drops because they do not
start with a 9-digit NIPC. Source run: `bin/ingest-postgres.sh`, 2026-11-16, data `2012`–`2026-11`.

> `contract_adjudicatarios: 238837 of 2337079 adjudicatarios segments discarded (no leading 9-digit NIPC)`

## Summary

- **238,832 of the 238,837 discarded entries have NIF `-` in the source.** The portal export writes
  `nifEntContrato=-`; the transform turns that into `- - <name>`. The other 5 are malformed NIPCs.
- **The claim "mostly foreign companies and private individuals" is only half right.**
  - **Mostly private individuals: yes.** About 81% of the entries are Portuguese people.
  - **Foreign companies: a minority by count (17%), but most of the money** (75% of the value of the
    affected contracts).
- **Why the NIF is missing:** the portal never publishes an individual's NIF. No valid NIPC in the
  data starts with `1`, `2`, `3` or `4` (the individual ranges). Foreign companies have no Portuguese
  NIPC, so they also get `-`.
- **Impact:** 99% of these entries are the contract's only awardee. About 237k contracts (10.4%) have no
  row in `procurement.contract_adjudicatarios`. They hold €10.97bn of the €159.4bn total (6.9%).
- **The warning text is misleading.** The count grows each year with total volume (6.6k contracts in
  2012, 26.8k in 2025). It is not an upstream format change.

## Method

- Input: the raw yearly exports `data/raw/contracts_raw_*.parquet`, deduplicated on `idcontrato`
  (2,293,018 contracts, 2,337,079 awardee entries — the same totals as the ingest run).
- Each `adjudicatarios` line is a key=value record. Fields used: `nifEntContrato`, `descEntContrato`
  (name), `codPaisEntContrato` (country, ISO 3166 alpha-3). The transform keeps only NIF and name, so
  the country is not in `contracts.parquet` or Postgres.
- **Origin** comes from `codPaisEntContrato`: `PRT`, foreign (any other code) or null.
- **Kind** comes from the name, with regex heuristics, in this order:
  1. *foreign legal form* — `S.L.`, `S.L.U.`, `GmbH`, `Ltd`, `Limited`, `B.V.`, `Inc`, `Sucursal`, …
  2. *PT-style company/org* — `Lda`, `Unipessoal`, `S.A.`, `Associação`, `Fundação`, `&`, sector
     words, …
  3. *person (name-like)* — letters only, 2–8 words, no legal form.
  4. *other/unclear* — everything else (brand names, digits, mixed forms).

  The heuristic is approximate. Its errors are listed under each bucket below.

### Reproduce

```bash
uv run python docs/no_nipc_awardees.py            # reads data/raw/contracts_raw_*.parquet
uv run python docs/no_nipc_awardees.py --raw-dir <path/to/raw>
```

The script prints every table and number used in this report.

## Breakdown

### By origin and kind (entries)

| Kind | `PRT` | Foreign | Null | Total |
|---|---:|---:|---:|---:|
| Person (name-like) | 194,009 | 9,284 | 17 | 203,310 |
| Foreign legal form | 44 | 21,269 | 13 | 21,326 |
| PT-style company/org | 2,357 | 7,972 | 2 | 10,331 |
| Other/unclear | 1,653 | 2,211 | 1 | 3,865 |
| **Total** | **198,063 (82.9%)** | **40,736 (17.1%)** | **33** | **238,832** |

### By origin (distinct contracts and value)

| Origin | Contracts | Value (€) | Median contract (€) |
|---|---:|---:|---:|
| `PRT` | 197,346 | 2.70bn | 8,760 |
| Foreign | 40,368 | 8.27bn | 13,550 |
| Null | 33 | 0.86m | 12,000 |

### Foreign countries (top 12 of the 40,736 foreign entries)

| ESP | GBR | DEU | FRA | USA | NLD | BEL | ITA | IRL | CHE | AFG | AND |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 18,201 | 4,021 | 3,271 | 2,640 | 2,114 | 1,557 | 1,033 | 866 | 752 | 636 | 475 | 453 |

Spain alone is 45% of the foreign entries.

## Examples

### `PRT` · person — 194,009 entries, 85,436 distinct names

The largest group by far. 76% of these names start with a common Portuguese first name from a list of
~170 (Ana, Maria, João, José, …). That is a lower bound, because the list is not complete.

| Most frequent | Entries |
|---|---:|
| Mário Paulo Ferreira | 629 |
| Miguel Luís Teixeira Guedes | 371 |
| Mário Paulo Moura Silva Ferreira | 176 |
| Avelino da Silva Dores | 170 |
| NUNO MIGUEL DOS SANTOS PIMENTEL | 140 |
| ARMANDO COSTA OLIVEIRA DA FONSECA | 135 |

- Random sample: *Telmo Manuel Pais Fonseca*, *Sandra Raquel Louro de Andrade*, *Mónica Sofia
  Gonçalves Salgado*, *António Guilherme Gorducho Baldaia*.
- Mostly small direct awards: Ajuste Direto Regime Geral 146,309 contracts, Consulta Prévia 25,811,
  Contratação excluída II 13,588.
- Heuristic error: some sole-trader names fall into this bucket (e.g. *Gartécnica - Paulo Sérgio
  Correia Garcia*, 138). These are individuals trading under a name, so they are still people
  without a published NIF.

### Foreign · foreign legal form — 21,269 entries

| Most frequent | Country | Entries |
|---|---|---:|
| CEPHEID IBERIA, S.L.U. (SUCURSAL EM PORTUGAL) | ESP | 420 |
| TERUMO EUROPE ESPANHA SL | ESP | 330 |
| LELEMAN S.L. | ESP | 241 |
| Terumo Europe España S.L. | ESP | 214 |
| EBSCO Information Services S.L.U. | ESP | 214 |
| CLINIGEN IRELAND LIMITED | IRL | 209 |
| Penumbra Europe GMBH | DEU | 168 |
| B2 MOBILITY, GMBH | DEU | 168 |

Mostly medical and pharma suppliers. The same company often appears under several spellings (Terumo
has at least 4).

### Foreign · no legal form in the name — 9,284 + 2,211 entries

These are companies, not people. The heuristic is wrong here. Examples: *TERUMO EUROPE ESPAÑA* (165),
*RECORDATI RARE DISEASES* (91), *WEP CLINICAL* (78), *THYSSENKRUPP MARINE SYSTEMS* (71), *Dassault
Aviation* (111 across two spellings), *LELEMAN* (139), *MDPI* (23). There are a few real foreign
individuals, e.g. *André Novas Otero* (ESP).

### Largest foreign contracts

| Awardee | Country | Value (€) | Year |
|---|---|---:|---:|
| FCC Construcción, S.A. | ESP | 379.5m | 2023 |
| VEOLIA ENERGIE INTERNACIONAL, SA. | FRA | 267.1m | 2025 |
| FERROVIAL CONSTRUCCIÓN, S.A. | ESP | 189.1m | 2020 |
| NEX CONTINENTAL HOLDINGS, SLU | ESP | 184.7m | 2021 |
| Stadler Service Nederland B.V. | NLD | 158.1m | 2021 |
| ACCIONA CONSTRUCCIÓN, SA. | ESP | 148.9m | 2020 |
| Alcatel Submarine Networks | FRA | 139.0m | 2024 |

### `PRT` · company/org — 2,357 + 44 entries

Portuguese companies that do have an NIPC, but the record does not include it. Examples:
*Smartcais Construção Unipessoal Lda* (39 across two spellings), *PMH - Produtos Médico Hospitalares,
S.A.* (22), *Padez Caetano Terraplanagens, Lda*. 536 of these entries match exactly one NIPC by name
elsewhere in the data, so the NIPC could be recovered.

There is some noise: *Aquisição de Serviços de Formação Profissional* (44) is a contract description
in the name field.

## Data-quality findings

1. **Wrong country codes.** Some Portuguese companies are coded as foreign countries. 2,357
   foreign-coded entries have `Lda`/`Unipessoal` in the name. The codes that look like picks from an
   alphabetical dropdown are the worst:

   | Code | Entries | Top names |
   |---|---:|---|
   | AFG (Afghanistan) | 475 | PVL - Produtos para Laboratório, Lda; Novabit - Informática, Unipessoal, Lda |
   | AND (Andorra) | 453 | Fresenius Kabi Pharma Portugal, Lda; Hikma Farmacêutica (Portugal), S.A.; NOS Comunicações, S.A. |
   | EGY (Egypt) | 261 | Higibras, Lda; Garrafeira Soares, Lda |
   | ALB (Albania) | 57 | Tecnovia - Madeira, S.A.; MC Computadores, S.A. |
   | AGO (Angola) | 49 | Orona Portugal, Lda; Otis Elevadores, Lda |

   So the foreign share (17.1%) is a slight overstatement.
2. **The same entity under several spellings.** A name-based fallback key would need normalisation
   first (Terumo: *TERUMO EUROPE ESPANHA SL*, *Terumo Europe España S.L.*, *TERUMO EUROPE ESPAÑA*,
   *TERUMO EUROPE ESPANA SL (PORTUGAL)*).
3. **5 malformed NIPCs** (not `-`): `516 871 498` (×2) and `502 544 180` have spaces and can be
   recovered. `51825482` and `51372670` have only 8 digits and cannot be recovered safely.
   Two more malformed NIFs pass the bridge's `^\d{9}` prefix match, so the bridge keeps their first
   9 digits: `507857542_1` (Gold Energy; its stored name becomes `_1 - Gold Energy …`) and
   `502496878` followed by a tab (Construções Pragosa; parsed correctly).
4. **33 entries have no country code.**

## Implications for the ingest

- The `RAISE WARNING` in `30_rebuild_bridge.sql` fires on every run because of the blank-NIF entries.
  A real format change would not stand out. Count `-` entries separately and keep the warning for
  real parse failures (5 this run).
- `codPaisEntContrato` is dropped in `canonical_packed`. Keeping it would allow foreign / domestic
  analysis and would show the miscoded entries.
- Any per-awardee analysis built on `contract_adjudicatarios` misses ~10% of contracts. That is
  mostly individuals on small direct awards, plus most of the foreign spend. Name-based keys for
  people raise privacy questions: the portal hides their NIFs on purpose.
