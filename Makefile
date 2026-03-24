SKILLS_DIR := $(HOME)/.claude/skills
SKILL_DIRS := $(shell find . -maxdepth 2 -name SKILL.md -not -path './.git/*' | sed 's|/SKILL.md||; s|^\./||')

.PHONY: install uninstall list check

install: $(SKILLS_DIR)
	@# Migrate legacy skills if they exist
	@for legacy in cpr blau-cpr; do \
		if [ -L "$(SKILLS_DIR)/$$legacy" ] || [ -d "$(SKILLS_DIR)/$$legacy" ]; then \
			rm -rf "$(SKILLS_DIR)/$$legacy"; \
			echo "Migrated: removed legacy $$legacy"; \
		fi; \
	done
	@for skill in $(SKILL_DIRS); do \
		ln -sfn "$(CURDIR)/$$skill" "$(SKILLS_DIR)/$$skill"; \
		echo "Linked: $$skill -> $(SKILLS_DIR)/$$skill"; \
	done
	@echo "Done. $(words $(SKILL_DIRS)) skill(s) installed."

uninstall:
	@for skill in $(SKILL_DIRS); do \
		if [ -L "$(SKILLS_DIR)/$$skill" ]; then \
			rm "$(SKILLS_DIR)/$$skill"; \
			echo "Unlinked: $$skill"; \
		fi; \
	done
	@echo "Done."

list:
	@for skill in $(SKILL_DIRS); do \
		if [ -L "$(SKILLS_DIR)/$$skill" ]; then \
			echo "[installed] $$skill"; \
		else \
			echo "[missing]   $$skill"; \
		fi; \
	done

check:
	@ok=true; \
	for skill in $(SKILL_DIRS); do \
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
