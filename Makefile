PYTHON ?= python3
PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
WORKDIR ?= $(CURDIR)
ARGS ?=
BIN_DIR ?= $(HOME)/.local/bin

VENV_DIR := $(PROJECT_DIR)/.venv
ifeq ($(OS),Windows_NT)
VENV_BIN := $(VENV_DIR)/Scripts
VENV_PYTHON := $(VENV_BIN)/python.exe
CLI_SUFFIX := .exe
else
VENV_BIN := $(VENV_DIR)/bin
VENV_PYTHON := $(VENV_BIN)/python
CLI_SUFFIX :=
endif

.PHONY: start install-global uninstall-global

start:
	@$(PYTHON) -m pip install -e "$(PROJECT_DIR)" --quiet
	@cd "$(WORKDIR)" && $(PYTHON) -m gitferret $(ARGS)

install-global:
	@if [ ! -x "$(VENV_PYTHON)" ] || ! "$(VENV_PYTHON)" -c 'import sys' >/dev/null 2>&1; then \
		$(PYTHON) -m venv "$(PROJECT_DIR)/.venv"; \
	fi
	@"$(VENV_PYTHON)" -m pip install -e "$(PROJECT_DIR)" --quiet
	@mkdir -p "$(BIN_DIR)"
	@ln -sf "$(VENV_BIN)/gitferret$(CLI_SUFFIX)" "$(BIN_DIR)/gitferret"
	@ln -sf "$(VENV_BIN)/git-ferret$(CLI_SUFFIX)" "$(BIN_DIR)/git-ferret"
	@echo "Installed globally to $(BIN_DIR): gitferret, git-ferret (git ferret)"

uninstall-global:
	@rm -f "$(BIN_DIR)/gitferret" "$(BIN_DIR)/git-ferret"
	@echo "Removed symlinks from $(BIN_DIR)"
