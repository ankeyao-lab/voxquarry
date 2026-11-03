PYTHON ?= python3.11
VENV_PYTHON := .venv/bin/python

.PHONY: install start check test web-check release-check package
install:
	$(PYTHON) -m venv .venv
	$(VENV_PYTHON) -m pip install -r requirements.lock.txt
	npm --prefix web ci
	npm --prefix web run build
start:
	$(VENV_PYTHON) -m voxquarry
check: test web-check
	$(VENV_PYTHON) scripts/release_scan.py
	git diff --check
test:
	$(VENV_PYTHON) scripts/check.py
web-check:
	npm --prefix web run build
	npm --prefix web run test:sites
release-check: check
	$(VENV_PYTHON) scripts/release_scan.py --tracked
package: release-check
	$(VENV_PYTHON) scripts/package_release.py
