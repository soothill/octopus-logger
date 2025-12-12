APP_NAME=octopus-logger
SERVICE_FILE=systemd/$(APP_NAME).service
SERVICE_TEMPLATE=systemd/$(APP_NAME).service.template
SERVICE_RENDERER=scripts/render_systemd_service.py
INSTALL_PATH=/etc/systemd/system/$(APP_NAME).service
PYTHON=python3

.PHONY: install uninstall start stop test render-service help

render-service:
	@echo "Rendering systemd service from config/config.yaml..."
	@if [ ! -f config/config.yaml ]; then echo "Error: config/config.yaml not found."; exit 1; fi
	@./.venv/bin/python $(SERVICE_RENDERER) --config config/config.yaml --template $(SERVICE_TEMPLATE) --output $(SERVICE_FILE)

install:
	@echo "Installing $(APP_NAME)..."
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	@$(MAKE) render-service
	sudo cp $(SERVICE_FILE) $(INSTALL_PATH)
	sudo systemctl daemon-reload
	sudo systemctl enable $(APP_NAME)
	sudo systemctl restart $(APP_NAME) || sudo systemctl start $(APP_NAME)
	@echo "$(APP_NAME) installed and started."

uninstall:
	@echo "Uninstalling $(APP_NAME)..."
	sudo systemctl stop $(APP_NAME) || true
	sudo systemctl disable $(APP_NAME) || true
	sudo rm -f $(INSTALL_PATH)
	sudo systemctl daemon-reload
	@echo "$(APP_NAME) uninstalled."

start:
	sudo systemctl start $(APP_NAME)

stop:
	sudo systemctl stop $(APP_NAME)

test:
	@echo "Validating configuration..."
	@if [ ! -f config/config.yaml ]; then echo "Error: config/config.yaml not found."; exit 1; fi
	@if ! grep -q "api_key" config/config.yaml; then echo "Error: Missing octopus.api_key in config/config.yaml"; exit 1; fi
	@if ! grep -q "account_number" config/config.yaml; then echo "Error: Missing octopus.account_number in config/config.yaml"; exit 1; fi
	@if ! grep -q "url" config/config.yaml; then echo "Error: Missing influxdb.url in config/config.yaml"; exit 1; fi
	@if ! grep -q "token" config/config.yaml; then echo "Error: Missing influxdb.token in config/config.yaml"; exit 1; fi
	@if ! grep -q "org" config/config.yaml; then echo "Error: Missing influxdb.org in config/config.yaml"; exit 1; fi
	@if ! grep -q "bucket" config/config.yaml; then echo "Error: Missing influxdb.bucket in config/config.yaml"; exit 1; fi
	@echo "Configuration validation passed."
	@echo "Rendering systemd unit (sanity check)..."
	@$(MAKE) render-service
	@echo "Running $(APP_NAME) once for testing..."
	@./.venv/bin/python scripts/test_run.py
	@echo "Test completed successfully."

help:
	@echo "Available make targets:"
	@echo "  make install         - Install dependencies, render unit from config, install systemd service, enable & start"
	@echo "  make render-service  - Render systemd unit from config/config.yaml"
	@echo "  make uninstall       - Stop and remove the systemd service"
	@echo "  make start           - Start the systemd service"
	@echo "  make stop            - Stop the systemd service"
	@echo "  make test            - Run the logger once for testing"
	@echo "  make help            - Show this help message"
