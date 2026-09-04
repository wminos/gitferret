PYTHON ?= python3
PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
WORKDIR ?= $(CURDIR)
ARGS ?=
BIN_DIR ?= $(HOME)/.local/bin

.PHONY: start install-global uninstall-global

start:
	@$(PYTHON) -m pip install -e "$(PROJECT_DIR)" --quiet
	@cd "$(WORKDIR)" && $(PYTHON) -m gitferret $(ARGS)

install-global:
	@if [ ! -d "$(PROJECT_DIR)/.venv" ]; then \
		$(PYTHON) -m venv "$(PROJECT_DIR)/.venv"; \
	fi
	@"$(PROJECT_DIR)/.venv/bin/pip" install -e "$(PROJECT_DIR)" --quiet
	@mkdir -p "$(BIN_DIR)"
	@ln -sf "$(PROJECT_DIR)/.venv/bin/gitferret" "$(BIN_DIR)/gitferret"
	@ln -sf "$(PROJECT_DIR)/.venv/bin/git-ferret" "$(BIN_DIR)/git-ferret"
	@echo "Installed globally to $(BIN_DIR): gitferret, git-ferret (git ferret)"

uninstall-global:
	@rm -f "$(BIN_DIR)/gitferret" "$(BIN_DIR)/git-ferret"
	@echo "Removed symlinks from $(BIN_DIR)"
