# Launch and onboarding checklist

Working list for taking `argus-atlas` from spec to a package people find, try, and keep. Ordered so that each phase produces something the next phase links to. Do not announce before Phase 3 exists; each community gives one first impression.

Settled decisions are summarised in `CLAUDE.md` (What this is) and the README's "Not in v0" section; the original handoff document is not committed. This file is only about getting the finished thing in front of users.

## Phase 0: ship the thing (handoff build order)

- [x] Package skeleton, `doctor`, GDAL argv helpers, tests on a synthetic DEM
- [x] `controls` and the job sidecar
- [x] Alignment gate and `tiles` with a fake painted PNG
- [x] ComfyUI backend and `bake` behind `ATLAS_ALLOW_BAKE`
- [x] FastAPI wrapper and the 403 read-only behaviour
- [x] Dockerfile on the osgeo/gdal 3.11.4 image, CI green with 48 tests (17 Oct 2026)
- [x] `git init` and first push (17 Oct 2026)
- [ ] First tag `v0.1.0a1` so `hatch-vcs` versions a build and the PyPI name is held (needs the trusted publisher registered first, Phase 2)
- [x] First real AOI through the alignment gate (Lake Garda, 17 Oct 2026)

## Phase 1: first real bake becomes the hero asset

- [x] Pick a recognisable AOI with strong relief and a coastline (Lake Garda; then Rockingham, Rottnest, Albany, Stirling Range, 23 Oct 2026)
- [x] Bake it. Job dirs kept under `/home/smk/argus/atlas-jobs` on morpheus; contact sheets in `docs/img/`
- [x] Side-by-side hero (`docs/img/hero.jpg`: hillshade, composite, bake of Albany) at the top of the README via an absolute raw.githubusercontent.com URL (23 Oct 2026)
- [x] Opacity GIF (`docs/img/hero-drape.gif`, viewer.html over OpenFreeMap, 18 frames) in the README (23 Oct 2026)
- [x] Bundle a tiny sample DEM in `examples/` (`sample_dem.tif`, Copernicus GLO-30 clip, 350 KB, attributed) and make the README quick-start run against it with no edits (23 Oct 2026)

## Phase 2: make the PyPI page convert

- [x] `pyproject.toml` classifiers, keywords, project URLs (17 Oct 2026; review before first publish)
- [x] README long description renders on PyPI: `twine check` passes for sdist and wheel; repo-relative links made absolute (23 Oct 2026)
- [x] Hero image at the top of the README via an absolute URL
- [ ] Badges: PyPI version, Python versions, license, CI, and the OpenSSF or attestation badge once available
- [x] Repo public at https://github.com/smk762/argus-atlas (17 Oct 2026)
- [ ] Trusted Publishing: `.github/workflows/release.yml` (from `argus-pkg-template`) already publishes to TestPyPI then PyPI on `v*` tags via OIDC. Register the publisher on pypi.org and test.pypi.org (owner `smk762`, repo `argus-atlas`, workflow `release.yml`, environments `pypi` and `testpypi`) before the first tag, or the rehearsal job fails loudly.
- [x] `attestations: true` on the `publish-pypi` step (23 Oct 2026; also push it upstream to `argus-pkg-template`)
- [x] `CHANGELOG.md` kept current so the Changelog project URL is never a dead link
- [ ] Reserve the `argus-atlas` name on PyPI with the first alpha as soon as `controls` works, so the name is held and the page exists to link to

## Phase 3: a demo people can click

- [ ] One baked PMTiles file on static hosting (Hetzner CX23 or the homelab, see handoff hosting note). No server process, just the file and a page.
- [x] A MapLibre page with the fantasy overlay on a real basemap and an opacity slider (`examples/viewer.html`; loads from the API or a local `.pmtiles`). Still to do for hosting: pin the overlay URL and measure the load time.
- [ ] Demo URL goes in the README "Live demo" section and the `Demo` project URL in `pyproject.toml`
- [ ] Demo page footer links back to PyPI and GitHub

## Phase 4: onboarding paths for each audience

- [ ] **Local GPU user:** README quick start, sample DEM, packaged ComfyUI workflow template, `doctor` output pasted in the README so they know what healthy looks like
- [ ] **No GPU:** a Colab or Lightning notebook that runs `controls` and `tiles` on the sample DEM, and `bake` against a hosted ComfyUI if one is available. Link from README.
- [ ] **ComfyUI user:** publish the packaged workflow JSON on OpenArt and comfyworkflows with a one-paragraph description and the hero image
- [ ] **LoRA user:** Civitai article next to the cartography LoRA explaining that this package turns it into a georeferenced overlay
- [ ] **Studio user:** `/atlas` page in `argus-studio` (handoff "Studio follow-up"), only after CLI and `/health` work
- [ ] **ComfyUI-native user:** a standalone `georef` command that stamps and gates a PNG painted outside atlas against an existing job sidecar, so the workflow JSON can be used in ComfyUI directly and the package still owns the coordinates
- [ ] Error messages for the three common failures (GDAL missing, ComfyUI unreachable, AOI too large for the canvas) each say what to do next
- [ ] `argus-atlas doctor` output is copy-pasteable into an issue

## Phase 5: listings (one PR each, steady long-tail traffic)

- [ ] awesome-geospatial
- [ ] awesome-gdal
- [ ] awesome-comfyui
- [ ] awesome-stable-diffusion
- [ ] Protomaps docs: ask to be listed among tools that write PMTiles
- [ ] conda-forge feedstock (GDAL users install from conda and it solves the system-dependency problem for them)
- [ ] Hugging Face: model card for the cartography LoRA that links the package, and a Space if the no-GPU notebook can be hosted there

## Phase 6: the one blog post and launch week

- [ ] Write one technical post. Hook: "diffusion models lose your coordinates; here is how to get them back." Cover the sidecar, the alignment gate, a failure case where a one-pixel-off output is refused, and the PMTiles result. Link the demo and the package.
- [ ] Publish on own site first, then syndicate to dev.to
- [ ] Show HN, weekday morning US time, concrete title with the live map in the first line of the comment
- [ ] Lobsters (geospatial and ai tags)
- [ ] Same week, open the community threads in Phase 7. Do not spread the launch over a month.

## Phase 7: communities, with a tailored message each

Geospatial:
- [ ] Protomaps Discord (a real downstream PMTiles user is news to them)
- [ ] GDAL and Rasterio GitHub Discussions
- [ ] r/gis
- [ ] OSGeo lists if there is a FOSS4G talk to point at
- [ ] FOSS4G or Protomaps community call talk proposal

Generative:
- [ ] r/StableDiffusion and r/comfyui, with the workflow JSON attached
- [ ] Civitai article (Phase 4)

Worldbuilding (the surprise audience; they have done this by hand in Photoshop for years):
- [ ] r/worldbuilding
- [ ] r/mapmaking
- [ ] Cartographers' Guild forum
- [ ] Azgaar Fantasy Map Generator Discord

## Phase 8: measure and iterate

- [ ] pypistats or pepy for download trend, checked weekly for the first month
- [ ] GitHub traffic referrers to see which channel actually sent people
- [ ] Count issues by failure class; fold the top one into the README or `doctor` each week
- [ ] Ask the first five real users what they tried to bake and whether it lined up. Their AOIs become the gallery.

## Do not

- Announce before Phase 3. The demo link is the message.
- Lead with the whole Argus suite. Atlas is the one with a picture.
- Pay for promotion or post to PyPI aggregator sites or Product Hunt. The audience is too specific.
- Put the resume line in a resume before a real AOI has passed the gate (handoff rule).
