# nodeyard development tasks. Run 'make help' for the list.

SHELL := bash
VERSION ?= $(shell sed -n 's/^NY_VERSION="\(.*\)"$$/\1/p' lib/core/base.sh)
SHELL_FILES := $(shell git ls-files '*.sh' '*.bash' 2>/dev/null) bin/nodeyard install.sh uninstall.sh
BATS := .tools/bin/bats
export PATH := $(CURDIR)/.tools/bin:$(PATH)

.DEFAULT_GOAL := help
.PHONY: help deps lint fmt test test-py harness demo build build-macos-ai-app yardcode release screenshots clean

help: ## Show this list
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

deps: ## Fetch the pinned test/lint tools into .tools/
	@tests/install-tools.sh

lint: ## shellcheck and shfmt (formatting check) on every shell file
	@command -v shellcheck >/dev/null || { echo "shellcheck not found: run 'make deps' (Linux) or 'brew install shellcheck'"; exit 1; }
	@command -v shfmt >/dev/null || { echo "shfmt not found: run 'make deps' (Linux) or 'brew install shfmt'"; exit 1; }
	shellcheck -x $(sort $(SHELL_FILES))
	shfmt -i 4 -ci -d $(sort $(SHELL_FILES))
	@for f in share/nodeyard/wizards/*.json; do jq -e . "$$f" >/dev/null || { echo "invalid JSON: $$f"; exit 1; }; done
	@echo "lint: ok"

fmt: ## Reformat every shell file with shfmt
	shfmt -i 4 -ci -w $(sort $(SHELL_FILES))

test: ## Run the bats unit tests (no root or hardware needed)
	@test -x $(BATS) || { echo "bats not found: run 'make deps' first"; exit 1; }
	$(BATS) tests/unit

test-py: ## Run the Python tests (dashboard, node agent, model gate, yardcode)
	python3 -W ignore -m unittest discover -s tests/dashboard -p 'test_*.py'
	cd tests/yardcode && python3 -W ignore -m unittest discover -s . -p 'test_*.py'

harness: ## Install and exercise nodeyard in a container per supported distro (needs Docker)
	tests/harness/run.sh $(DISTROS)

demo: ## Try nodeyard against a simulated cluster (changes nothing)
	bin/nodeyard --demo

build: ## Build release tarballs and SHA256SUMS into dist/
	scripts/build-release.sh $(VERSION)

build-macos-ai-app: ## Build the native Nodeyard AI macOS app
	scripts/build-macos-ai-app.sh

yardcode: ## Build the single-file yardcode program into dist/yardcode
	scripts/build-yardcode.sh dist/yardcode

release: ## Tag a release: make release VERSION=X.Y.Z (then push the tag)
	scripts/release.sh $(VERSION)

screenshots: ## Regenerate docs/media/*.svg from demo mode
	scripts/screenshots.sh

clean: ## Remove build output
	rm -rf dist tests/harness/logs
