PYTHON ?= python3

.PHONY: test check install uninstall help

help:
	@echo "make test       run the unit tests (stdlib unittest)"
	@echo "make check      syntax-check the python and shell files"
	@echo "make install    sudo ./install.sh"
	@echo "make uninstall  sudo ./install.sh --uninstall"

test:
	$(PYTHON) -m unittest -v test_dae_cli

check:
	$(PYTHON) -m py_compile dae_cli.py test_dae_cli.py
	sh -n dae-cli-menu
	bash -n install.sh
	@echo "syntax ok"

install:
	sudo ./install.sh

uninstall:
	sudo ./install.sh --uninstall
