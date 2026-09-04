# Third-party notices

This project is licensed under the Apache License 2.0 — see [LICENSE](LICENSE).
This file is the inventory of third-party software it builds on: the Python
dependencies declared in `requirements.txt` (version ranges) with the transitive
packages they resolve to in the shipped image, and the base image. Versions
below reflect a fresh resolution of the current ranges (`openai` ≥ 3 pulls the
`httpx2` fork, so both httpx families appear); licenses are taken from the
PyPI JSON API.

## Runtime dependencies (declared in `requirements.txt`, shipped in the image)

| Library | Version | License | Upstream |
| --- | --- | --- | --- |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |
| httpx2 | 2.12.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| beautifulsoup4 | 4.15.0 | MIT | https://www.crummy.com/software/BeautifulSoup/bs4/ |
| pydantic | 2.13.5 | MIT | https://github.com/pydantic/pydantic |
| pydantic-settings | 2.15.0 | MIT | https://github.com/pydantic/pydantic-settings |
| structlog | 25.5.0 | MIT OR Apache-2.0 | https://github.com/hynek/structlog |
| openai | 3.8.0 | Apache-2.0 | https://github.com/openai/openai-python |

## Transitive dependencies (installed in the image via the above)

| Library | Version | License | Upstream |
| --- | --- | --- | --- |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anyio | 4.15.0 | MIT | https://github.com/agronholm/anyio |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| httpcore | 1.0.9 | BSD-3-Clause | https://github.com/encode/httpcore |
| httpcore2 | 2.12.0 | BSD-3-Clause | https://github.com/pydantic/httpx2 |
| idna | 3.19 | BSD-3-Clause | https://github.com/kjd/idna |
| jiter | 0.16.0 | MIT | https://github.com/pydantic/jiter/ |
| pydantic_core | 2.46.5 | MIT | https://github.com/pydantic/pydantic |
| python-dotenv | 1.2.3 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | https://github.com/python-trio/sniffio |
| soupsieve | 2.9.2 | MIT | https://github.com/facelessuser/soupsieve |
| truststore | 0.10.4 | MIT | https://github.com/sethmlarson/truststore |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |

## Dev/test-only dependencies (`requirements-dev.txt`; host venv + CI, not in the prod image)

| Library | Version | License | Upstream |
| --- | --- | --- | --- |
| pytest | 9.1.1 | MIT | https://github.com/pytest-dev/pytest |
| pytest-mock | 3.15.1 | MIT | https://github.com/pytest-dev/pytest-mock/ |
| respx | 0.23.1 | BSD-3-Clause | https://lundberg.github.io/respx/ |
| iniconfig | 2.3.0 | MIT | https://github.com/pytest-dev/iniconfig |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| pluggy | 1.6.0 | MIT | https://github.com/pytest-dev/pluggy |
| Pygments | 2.21.0 | BSD-2-Clause | https://github.com/pygments/pygments |

## Base image

| Image | Contents license | Upstream |
| --- | --- | --- |
| `python:3.14-slim-bookworm` | CPython: PSF License Version 2; Debian base: DFSG-free | https://www.python.org / https://www.debian.org |

## Notes

- Version ranges live in `requirements.txt` / `requirements-dev.txt`; the
  exact closure floats within them. Regenerate this table when the ranges
  change (licenses via the PyPI JSON API, e.g.
  `https://pypi.org/pypi/<name>/<version>/json`).
- The extractor talks to the backend's ingestion API over HTTP and to an
  OpenAI-compatible LLM endpoint; those are services it calls, not bundled
  software.