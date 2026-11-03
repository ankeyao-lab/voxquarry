# Third-party components

VoxQuarry's Apache-2.0 license covers project code and documentation unless a file says otherwise. It does not relicense its dependencies. License text and copyright notices collected from installed distributions are in [third_party/licenses](third_party/licenses/); exact package versions and declared licenses are in [inventory.json](third_party/inventory.json).

The main application uses FastAPI (MIT), Uvicorn (BSD), HTTPX (BSD), jsonschema (MIT), Requests (Apache-2.0), Patchright (Apache-2.0), React (MIT), Vite (MIT), Lucide (ISC, with retained Feather-derived MIT notices), react-markdown (MIT), and remark-gfm (MIT). Transitive libraries retain the licenses listed in the inventory. Packages are installed from their registries; their implementations are not vendored into this source tree.

The optional research images install the dependencies pinned in `runtime/harness/requirements-*.txt`, including the DeepSeek Harness SDK/runtime and document/analysis libraries. Image builds preserve installed package notices and installation metadata. Those optional images are not included in the source archive. Build and inspect their software inventory before redistributing image binaries.

The UI uses system font stacks. No proprietary font or retailer product imagery is bundled. The VoxQuarry brand mark is an AI-generated project asset; menu icons are Lucide assets. Screenshots contain synthetic data. Retailer names identify adapters; no retailer endorsement is implied.
