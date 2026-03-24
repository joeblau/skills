SKILLS_DIR := $(HOME)/.claude/skills
SKILL_DIRS := $(wildcard blau-*/SKILL.md)
SKILL_NAMES := $(patsubst %/SKILL.md,%,$(SKILL_DIRS))

.PHONY: install uninstall list check

install: $(SKILLS_DIR)
	@# Migrate legacy cpr skill if it exists
	@if [ -L "$(SKILLS_DIR)/cpr" ] || [ -d "$(SKILLS_DIR)/cpr" ]; then \
		rm -rf "$(SKILLS_DIR)/cpr"; \
		echo "Migrated: removed legacy cpr (replaced by blau-cpr)"; \
	fi
	@for skill in $(SKILL_NAMES); do \
		ln -sfn "$(CURDIR)/$$skill" "$(SKILLS_DIR)/$$skill"; \
		echo "Linked: $$skill -> $(SKILLS_DIR)/$$skill"; \
	done
	@echo "Done. $(words $(SKILL_NAMES)) skill(s) installed."

uninstall:
	@for skill in $(SKILL_NAMES); do \
		if [ -L "$(SKILLS_DIR)/$$skill" ]; then \
			rm "$(SKILLS_DIR)/$$skill"; \
			echo "Unlinked: $$skill"; \
		fi; \
	done
	@echo "Done."

list:
	@for skill in $(SKILL_NAMES); do \
		if [ -L "$(SKILLS_DIR)/$$skill" ]; then \
			echo "[installed] $$skill"; \
		else \
			echo "[missing]   $$skill"; \
		fi; \
	done

check:
	@ok=true; \
	for skill in $(SKILL_NAMES); do \
		link="$(SKILLS_DIR)/$$skill"; \
		if [ ! -L "$$link" ]; then \
			echo "MISSING: $$link (not installed)"; \
			ok=false; \
		elif [ ! -e "$$link" ]; then \
			echo "BROKEN:  $$link -> $$(readlink $$link)"; \
			ok=false; \
		else \
			echo "OK:      $$link"; \
		fi; \
	done; \
	$$ok || { echo "Run 'make install' to fix."; exit 1; }

$(SKILLS_DIR):
	mkdir -p $(SKILLS_DIR)
