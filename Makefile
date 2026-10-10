# nodeyard build tasks. Run 'make help' for the list.

SHELL := bash
VERSION ?= $(shell sed -n 's/^NY_VERSION="\(.*\)"$$/\1/p' lib/core/base.sh)

.DEFAULT_GOAL := help
.PHONY: help demo build build-macos-ai-app install-macos-ai-app yardcode clean

help: ## Show this list
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-21s %s\n", $$1, $$2}'

demo: ## Try nodeyard against a simulated cluster (changes nothing)
	bin/nodeyard --demo

build: ## Build release tarballs and SHA256SUMS into dist/
	scripts/build-release.sh $(VERSION)

build-macos-ai-app: ## Build the native Nodeyard AI macOS app into dist/
	scripts/build-macos-ai-app.sh

install-macos-ai-app: ## Build, check and install Nodeyard AI in /Applications (macOS)
	sh scripts/install-macos-ai-app.sh

yardcode: ## Build the single-file yardcode program into dist/yardcode
	scripts/build-yardcode.sh dist/yardcode

clean: ## Remove build output
	rm -rf dist
