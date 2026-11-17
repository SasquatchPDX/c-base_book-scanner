# Design 32: audiobooks

- **Issue:** [#32](https://github.com/SasquatchPDX/c-base_book-scanner/issues/32)
- **Status:** Proposed. Waiting for the owner to confirm the decisions in
  [Decisions to confirm](#decisions-to-confirm).
- **Related:** #28 (browse/facets), #7 (storage), #23 (unsupported formats).

## 1. Problem

A member suggested that the library tool could also manage audiobooks. The
issue asks for research and a decision: should this system hold audiobooks,
and if so, how do they fit the `Book`/`FileVariant` model, the ingestion
pipeline, metadata identification and delivery?

This document researches the formats, metadata sources, data modelling,
delivery and licensing, and then gives a recommendation.

## 2. Recommendation in short

**Do not build audiobook playback or management into this system now.**

1. **Phase A (no code).** If members want audiobooks, run
   **[Audiobookshelf](https://www.audiobookshelf.org/)** next to this
   system. It is a GPL-3.0, self-hosted audiobook server with
   chapter-aware streaming, progress sync, mobile apps, and metadata and chapter
   lookup. It supports OpenID Connect SSO; native LDAP is not documented.
   Connect it to c-base identity through an OIDC bridge in front of LDAP (for
   example Authelia, Authentik or Keycloak), or use local accounts. It reads
   audio from its own folder on the same NAS. This system does not ingest
   audio.
2. **Phase B (small, optional).** Show "also available as an audiobook" links
   on our Book pages. These are cross-references to Audiobookshelf items,
   matched by ISBN/ASIN or by a reviewed title and author match. They are not
   files in our archive.
3. **Phase C (only if A and B prove insufficient).** Native audiobook variants
   in this system, using the data model in section 6. It would need its own
   ingestion branch, a metadata provider and a streaming endpoint. That is a
   multi-sprint project that competes with Version 1 and ScanTailor-Web work.

Why:

- AGENTS.md asks us to "reuse mature open-source libraries and command-line
  tools wherever sensible" and to "avoid broadening the sprint scope because an
  adjacent feature looks tempting".
- Audiobook *playback* (resume position per user, chapter navigation, mobile
  offline sync, variable speed) is a solved problem in Audiobookshelf. It is
  not a solved problem in this codebase, and none of it helps the project's
  mission: digital library plus physical-book scanning.
- The Version 1 milestone does not include audiobooks and must not be delayed
  by them.

## 3. Research: formats and chapters

| Format | Typical shape | Chapters | Tools |
|---|---|---|---|
| **M4B** (MPEG-4 audio, AAC; the `.m4b` extension marks it as an audiobook) | One file per book, often 200 MB to 1 GB | QuickTime chapter track and/or Nero `chpl` atom | `ffprobe -show_chapters`, `mutagen` (MP4 tags), `tone`, `mp4v2` |
| **MP3 folder** | One file per chapter or per CD track, `01 - …mp3` … `NN - …mp3` | Implicit (one file is one chapter) or ID3v2 `CHAP`/`CTOC` frames | `mutagen` (ID3), `ffprobe` |
| **Single MP3** | One large file | ID3v2 Chapter Frame Addendum (`CHAP`/`CTOC`), rarely present | `mutagen`, `ffprobe` |
| **Ogg Vorbis / Opus** | One or more files | Vorbis-comment `CHAPTERxxx` convention | `mutagen`, `ffprobe` |
| **FLAC** | Rare for audiobooks | Cue sheet or `CHAPTERxxx` | `mutagen`, `ffprobe` |
| **AAX/AAXC** (Audible) | DRM-protected M4B | Yes | **Out of scope:** removing the DRM is not something this project does (section 7). |
| **CD rips with `.cue`** | Disc-structured | Cue tracks | `ffprobe` |

Consequences for a native implementation:

- **The unit of upload is a folder, not a file.** An "MP3 folder" audiobook is
  10 to 300 files that together make up *one* variant. The current intake
  treats each file as its own `ImportJob`/`FileVariant`, the same problem as
  #60 (chapter-split ebooks). Audio would need a *multi-file variant* concept:
  a manifest of parts with order and duration.
- **Hashing and deduplication.** Exact duplicate detection would hash each part
  plus a manifest hash. Two rips of the same CD at different bitrates are *not*
  duplicates, in the same way a PDF and an EPUB are not.
- **Size.** Audiobooks are 100 to 1,000× larger than an EPUB. 1,000 audiobooks
  at about 400 MB is about 400 GB on the NAS. The storage abstraction handles
  this, but the storage-copy and verification paths (`_verify_archive`,
  re-hashing) become hour-long jobs per bulk import.
- **Conversion.** MP3 folder to M4B (with chapters) is a common request.
  `ffmpeg` plus a chapter metadata file does it, and it fits the conversion
  graph as an audio subgraph. It does not mix with text formats: there is no
  EPUB↔M4B edge, and text-to-speech is out of scope.

## 4. Research: metadata sources

| Source | What it has | Access | Fit |
|---|---|---|---|
| **Audible** (catalogue) | Narrators, runtime, series, chapters, ASIN | No public API. Third-party tools use undocumented endpoints. | Terms-of-service risk. Do not call directly. |
| **Audnexus** (`api.audnex.us`) | An open-source aggregator of Audible data: books by ASIN, authors, chapters | Public, no key, about 100 requests/min reported. The software is self-hostable. | Good for *chapters and narrators* when an ASIN is known. It inherits the ToS ambiguity of Audible. Self-host it if used. |
| **Open Library** | Editions with `physical_format` (values vary: "Audio CD", "MP3 CD", "Audiobook"), sometimes narrators as contributors | Already integrated (`OpenLibraryMetadataProvider`) | Works for *work-level* identification (title and author). Weak for narration data. |
| **MusicBrainz** | Audiobook releases, with an official style guideline: one release group per reading, the author as "writer", narrators credited | Open API (CC0 core data), 1 request/s | Good for CD-ripped audiobooks with a disc ID or barcode. Coverage of download-only titles is limited. |
| **LibriVox** | Public-domain recordings, with readers and sections | Public API (`/api/feed/audiobooks`), with behaviour and filters that are poorly documented | Excellent for public-domain titles, and the recordings are themselves public domain. A safe "starter content" source. |
| **AudioSilo Meta** | A community CC0 audiobook metadata database (narrators, series, chapters) | Community-hosted endpoint, also usable as an Audiobookshelf custom provider | Promising, but young. Evaluate before relying on it. |
| **Hardcover** | Has audiobook editions with narrators in some records | Already an optional provider (`hardcover_provider.py`) | Possible secondary source. |
| **Embedded tags** | Title, artist/album artist (often the author), composer or `©nrt` (narrator, unreliable), year, cover art | `mutagen` | The primary *evidence* signal, like EPUB OPF. It is not truth. |

Conclusion: identifying the **work** (title and author) can reuse the existing
providers and scorer unchanged. **Narration data** (narrator, runtime,
chapters, ASIN) needs a new provider, Audnexus or MusicBrainz, behind the
existing `MetadataProvider` abstraction. That is the expensive part to build
natively, and Audiobookshelf already has it.

## 5. Streaming vs download

| | Download only | Streaming |
|---|---|---|
| Implementation here | Trivial: the existing `library_book_download` path with `FileResponse` | HTTP Range support (nginx `X-Accel-Redirect` handles it), an HTML `<audio>` player, per-user resume position, chapter navigation, and multi-file playlists for MP3 folders |
| Member value | Low for MP3 folders (a zip of 300 files) and fine for M4B | High: this is how people actually consume audiobooks |
| Mobile | The member manages the files | Needs an app or a PWA with offline caching. Audiobookshelf already has Android and iOS apps. |

Streaming is the feature members want, and it is the part that would cost the
most to rebuild. That is the main argument for Audiobookshelf.

## 6. Data model if audio ever lands here (phase C sketch)

This section exists so that a phase C decision does not need a redesign, and
to show that the Book/FileVariant split holds up.

**Keep `Book` as the bibliographic work/edition.** A narration is a
*representation* of the book, like a PDF or a scan, so it is a kind of
FileVariant. It is not a different Book. One difference matters: two different
narrators are two different *performances* with their own metadata, and that
is more than a format difference. Hence an `AudioEdition` grouping:

```python
class FileVariant.VariantFormat:     # add
    M4B = "m4b", "M4B audiobook"
    MP3_SET = "mp3_set", "MP3 audiobook (multi-file)"
    OPUS_SET = "opus_set", "Opus audiobook (multi-file)"

class FileVariant.MediaKind(models.TextChoices):   # new field, default TEXT -> no change for existing rows
    TEXT = "text"; IMAGE = "image"; AUDIO = "audio"

class AudioEdition(models.Model):
    """One narration/performance of a Book. Several FileVariants (M4B, MP3 set, re-encodes) may belong to it."""
    book = FK(Book, on_delete=CASCADE, related_name="audio_editions")
    narrators = ManyToManyField("Contributor", related_name="narrations")   # or reuse Author with a role
    abridged = BooleanField(null=True)
    runtime_seconds = PositiveIntegerField(null=True)
    language = CharField(max_length=32, blank=True)     # may differ from Book.language (translation read aloud)
    publisher = CharField(max_length=255, blank=True)   # audio publisher, e.g. Hörverlag
    release_year = PositiveSmallIntegerField(null=True)
    identifiers = JSON / or Identifier rows with kind ASIN / MBID / LibriVox id
FileVariant.audio_edition = FK(AudioEdition, null=True, on_delete=SET_NULL)

class AudioPart(models.Model):        # multi-file variants (MP3 folder): ordered manifest
    variant = FK(FileVariant, on_delete=CASCADE, related_name="audio_parts")
    sequence = PositiveIntegerField(); storage_identifier = CharField(512)
    sha256 = CharField(64); size_bytes = PositiveBigIntegerField(); duration_ms = PositiveIntegerField()

class AudioChapter(models.Model):
    variant = FK(FileVariant, on_delete=CASCADE, related_name="audio_chapters")
    index = PositiveIntegerField(); title = CharField(512, blank=True)
    start_ms = PositiveBigIntegerField(); end_ms = PositiveBigIntegerField(null=True)
    part = FK(AudioPart, null=True, on_delete=CASCADE)   # chapter inside which part
    source = CharField(choices=["embedded", "file_boundaries", "provider", "manual"])

class ListeningProgress(models.Model):        # only if we stream ourselves
    user = FK(User); variant = FK(FileVariant); position_ms = PositiveBigIntegerField(); updated_at = ...
```

Fitting the existing principles:

- **Book != File** holds. The narration is a representation. `AudioEdition`
  groups variants of one performance, the way `source_variant` groups derived
  text variants.
- **Preserve originals.** An MP3 set converted to M4B is a derived variant with
  `source_variant` and converter provenance, and the original parts are kept.
- **Ingestion.** A new "folder unit" import job type. Hashing per part and
  manifest happens in the background. The same state machine is used. Low
  confidence goes to review.
- **Provider abstraction.** `AudnexusProvider` and `MusicBrainzAudiobookProvider`
  implement `MetadataProvider`. Their candidates carry narrators and runtime in
  `raw_payload`.
- **Search and browse (#28).** "Audiobook" becomes a format facet value, and the
  search document gets a `has_audio` flag and narrator names (weight C).

## 7. Licensing and legal

- **DRM.** Most commercial audiobooks (Audible AAX/AAXC, many store downloads)
  are DRM-protected. Removing the protection is restricted by law: § 95a UrhG in
  Germany, Art. 6 of the EU InfoSoc Directive, 17 U.S.C. § 1201 in the US.
  **This system must never ship DRM-removal tooling or accept activation
  bytes.** Ingest only DRM-free files, and detect AAX/AAXC and reject them with
  a clear message.
- **Sharing.** Audiobooks raise the same copyright question as ebooks:
  members-only access, no public distribution. It is not a new class of risk,
  but the files are larger and many publishers enforce actively. The hackerspace
  should apply the same policy it applies to ebooks. **This is an owner and
  board decision, not a technical one.**
- **Low-risk starter content.** LibriVox dedicates its recordings to the public
  domain, and the texts are public domain *in the US*. A few texts may still be
  under copyright in Germany (author's life plus 70 years), so check the author's
  death year. They are a low-risk way to pilot audiobooks.
- **Metadata terms.** Audnexus data comes from Audible's catalogue, so self-host
  it and keep call volume low. MusicBrainz core data is CC0. Open Library data
  is open (CC0 for most bibliographic data).
- **Software licences.** Audiobookshelf and Audnexus are both GPL-3.0, as their
  GitHub repositories state. Running them unmodified as separate services
  creates no licensing obligation for this codebase. `mutagen` is GPL-2.0+ and
  `ffmpeg` is LGPL/GPL, depending on the build. Using either as a separate CLI
  or a library in a server we don't distribute is fine, but note it if the
  project is ever packaged.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Scope creep delays Version 1 and ScanTailor-Web | Phase A needs no code here. Phase C is gated on an explicit decision. |
| Storage growth (hundreds of GB) | A separate NAS folder and share for Audiobookshelf, with capacity planning by the operator |
| Two systems, two logins | OIDC bridge in front of LDAP, or Audiobookshelf local accounts. Phase B cross-links. |
| DRM or legal | Reject AAX/AAXC. No DRM tooling. Owner and board policy on sharing. |
| Metadata ToS (Audible-derived data) | Self-hosted Audnexus, low volume, and MusicBrainz/LibriVox/Open Library first |
| Duplicate identity across systems (a book in both) | Phase B matching through ISBN/ASIN; ambiguous cases become a review task |

## 9. Test strategy

- **Phase A:** no code. An operations doc page with deployment notes and an
  OIDC bridge recipe, reviewed by the operator.
- **Phase B:**
  - unit tests for the cross-reference matcher (ISBN/ASIN exact, title and
    author through the existing scorer, ambiguous cases going to review);
  - an integration test against a recorded Audiobookshelf API response
    (`/api/libraries/{id}/items`);
  - a permission test: links are only shown to authenticated members.
- **Phase C (if ever):**
  - fixture audio files generated in tests with `ffmpeg` (sine tones with
    chapter metadata), so no copyrighted content is needed;
  - unit tests for chapter extraction (M4B chpl/QuickTime, ID3 CHAP, file
    boundaries), part ordering (natural sort of `01`, `2`, `10`), and the
    manifest hash;
  - an AAX rejection test;
  - Range-request streaming tests;
  - a progress API test.

## 10. MVP and later

- **MVP:** phase A, Audiobookshelf deployed next to this system, and nothing
  in this codebase.
- **Later:** phase B (cross-links), if members use phase A. Phase C only on an
  explicit owner decision after phase A experience.

## 11. Phased implementation plan (issue-sized tasks)

1. **Ops: Audiobookshelf deployment note** (`docs/operations.md` section,
   optional).
   - Acceptance:
     - the deployment layout (separate NAS folder) is documented;
     - the OIDC bridge to LDAP, or local accounts, is documented;
     - the backup expectation is noted (handled externally, per AGENTS.md);
     - an explicit statement that this system does not ingest audio is
       included.
2. **Intake: recognise and skip audio cleanly.**
   - Scope: classify `.m4b`, `.m4a`, `.mp3`, `.aax`, `.aaxc`, `.opus`, `.ogg`
     and `.flac` in source scans as `skipped: audio (see Audiobookshelf)`
     instead of "unsupported format" noise (related to #23).
   - Acceptance:
     - audio files are listed in the source preview with that reason;
     - no ImportJob is created.
3. *(Phase B)* **Audiobookshelf cross-reference.**
   - Scope:
     - an `ExternalCatalogLink(book, system="audiobookshelf", external_id,
       match_method, verified_by)` model;
     - a sync job that reads the Audiobookshelf API with a read-only token;
     - matching through ISBN/ASIN or the scorer;
     - ambiguous matches become a `ReviewTask`;
     - an "Also available as an audiobook" link on the Book page.
   - Acceptance:
     - no automatic link below the auto-accept rules;
     - the token is never logged;
     - links appear only for members.
4. *(Phase C, separate decision)* Native audio. Break it down from section 6
   into these tasks:
   - the model and migration;
   - folder-unit ingestion;
   - chapter extraction;
   - the Audnexus/MusicBrainz providers;
   - the streaming endpoint and player;
   - progress sync;
   - MP3-set to M4B conversion.

## Decisions to confirm

1. Audiobooks do **not** belong in this system's archive for now. Members who
   want them get Audiobookshelf as a separate service (phase A).
2. Intake should explicitly recognise and skip audio files (task 2).
3. Whether phase B cross-links are wanted.
4. **Owner and board:** the sharing policy for commercial (DRM-free) audiobooks
   among members. The system will never handle DRM-protected files.

## Sources

- Audiobookshelf documentation (features, metadata and chapter lookup, M4B
  merge): <https://www.audiobookshelf.org/docs>
- Audiobookshelf, *OpenID Connect authentication* (OIDC since v2.6.0): <https://www.audiobookshelf.org/guides/oidc_authentication>
- Audiobookshelf, community metadata providers (includes AudioSilo Meta): <https://audiobookshelf.org/docs/documentation/community/community-providers>
- Audiobookshelf source and licence: <https://github.com/advplyr/audiobookshelf>
- Audnexus (open-source audiobook metadata aggregator): <https://github.com/laxamentumtech/audnexus>
- MusicBrainz style guideline, *Audiobook*: <https://musicbrainz.org/doc/Style/Specific_types_of_releases/Audiobook>
- MusicBrainz API (rate limiting): <https://musicbrainz.org/doc/MusicBrainz_API>
- LibriVox API: <https://librivox.org/api/info>
- Open Library Books API: <https://openlibrary.org/dev/docs/api/books>
- ID3v2 Chapter Frame Addendum: <https://id3.org/id3v2-chapters-1.0>
- Xiph, Vorbis comment chapter extension: <https://wiki.xiph.org/Chapter_Extension>
- FFmpeg `ffprobe` (`-show_chapters`): <https://ffmpeg.org/ffprobe.html>
- mutagen: <https://mutagen.readthedocs.io/>
- German Copyright Act § 95a (protection of technological measures): <https://www.gesetze-im-internet.de/urhg/__95a.html>
- US 17 U.S.C. § 1201: <https://www.law.cornell.edu/uscode/text/17/1201>
