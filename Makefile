# ============================================================================
#  Torrador de Cafe - Automacao de desenvolvimento e operacao
#  Uso rapido:
#    make setup      -> instala dependencias do backend e frontend
#    make dev        -> sobe backend + frontend (ESP32 real via TBEAM_HOST)
#    make dev-sim    -> sobe simulador + backend + frontend (sem ESP32)
#    make dev TBEAM_HOST=192.168.0.42
# ============================================================================

TBEAM_HOST ?= torrador.local
TBEAM_PORT ?= 80
BACKEND_PORT ?= 8000
FRONTEND_PORT ?= 5173

PYTHON := backend/.venv/bin/python
UVICORN := backend/.venv/bin/uvicorn

.DEFAULT_GOAL := help
.PHONY: help setup backend frontend simulador dev dev-sim

help:
	@echo "Comandos disponiveis:"
	@echo "  make setup       Cria backend/.venv e instala dependencias (Python + Node)"
	@echo "  make dev         Sobe backend + frontend juntos (Ctrl+C encerra os dois)"
	@echo "  make dev-sim     Sobe simulador + backend + frontend (para testar sem ESP32)"
	@echo "  make backend     Sobe apenas o backend (porta $(BACKEND_PORT))"
	@echo "  make frontend    Sobe apenas o frontend (porta $(FRONTEND_PORT))"
	@echo "  make simulador   Sobe apenas o simulador do T-Beam (porta 8080)"
	@echo ""
	@echo "Opcoes (podem ser passadas na linha de comando):"
	@echo "  TBEAM_HOST=$(TBEAM_HOST)  TBEAM_PORT=$(TBEAM_PORT)  BACKEND_PORT=$(BACKEND_PORT)  FRONTEND_PORT=$(FRONTEND_PORT)"

setup: backend/.venv/bin/uvicorn frontend/node_modules

backend/.venv/bin/uvicorn: backend/requirements.txt
	python3 -m venv backend/.venv
	backend/.venv/bin/pip install -r backend/requirements.txt

frontend/node_modules: frontend/package.json
	npm --prefix frontend install

backend: backend/.venv/bin/uvicorn
	TBEAM_HOST=$(TBEAM_HOST) TBEAM_PORT=$(TBEAM_PORT) $(UVICORN) backend.main:app --port $(BACKEND_PORT)

frontend: frontend/node_modules
	npm --prefix frontend run dev -- --port $(FRONTEND_PORT)

simulador: backend/.venv/bin/uvicorn
	$(PYTHON) backend/simulador_tbeam.py

dev: backend/.venv/bin/uvicorn frontend/node_modules
	@echo "Subindo backend (http://localhost:$(BACKEND_PORT)) e frontend (http://localhost:$(FRONTEND_PORT))..."
	@echo "T-Beam alvo: ws://$(TBEAM_HOST):$(TBEAM_PORT)/ws (Ctrl+C para encerrar)"
	@bash -c 'trap "kill 0" EXIT INT TERM; \
		TBEAM_HOST="$(TBEAM_HOST)" TBEAM_PORT="$(TBEAM_PORT)" $(UVICORN) backend.main:app --port "$(BACKEND_PORT)" & \
		npm --prefix frontend run dev -- --port "$(FRONTEND_PORT)" & \
		wait'

dev-sim: backend/.venv/bin/uvicorn frontend/node_modules
	@echo "Subindo simulador + backend (http://localhost:$(BACKEND_PORT)) + frontend (http://localhost:$(FRONTEND_PORT))..."
	@bash -c 'trap "kill 0" EXIT INT TERM; \
		$(PYTHON) backend/simulador_tbeam.py & \
		sleep 1; \
		TBEAM_HOST=127.0.0.1 TBEAM_PORT=8080 $(UVICORN) backend.main:app --port "$(BACKEND_PORT)" & \
		npm --prefix frontend run dev -- --port "$(FRONTEND_PORT)" & \
		wait'
