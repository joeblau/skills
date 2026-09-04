SKILLS_DIR := $(HOME)/.claude/skills
PLUGIN_DIR := b
SKILLS_SRC := $(PLUGIN_DIR)/skills
ALL_SKILLS := $(shell find $(SKILLS_SRC) -maxdepth 2 -name SKILL.md | sed 's|/SKILL.md||; s|^$(SKILLS_SRC)/||' | sort)

# Override to act on a subset: `make install SKILLS=cpr` or `make install SKILLS="cpr x-post"`
SKILLS ?= $(ALL_SKILLS)

INVALID := $(filter-out $(ALL_SKILLS),$(SKILLS))

.PHONY: install uninstall list check skills verify-skills validate

verify-skills:
	@if [ -n "$(INVALID)" ]; then \
		echo "Unknown skill(s): $(INVALID)"; \
		echo "Available: $(ALL_SKILLS)"; \
		exit 1; \
	fi

skills:
	@for skill in $(ALL_SKILLS); do echo "$$skill"; done

install: verify-skills $(SKILLS_DIR)
	@# Migrate legacy skill names that are no longer directories in this repo
	@for legacy in $(filter-out $(ALL_SKILLS),cpr blau-cpr); do \
		if [ -L "$(SKILLS_DIR)/$$legacy" ] || [ -d "$(SKILLS_DIR)/$$legacy" ]; then \
			rm -rf "$(SKILLS_DIR)/$$legacy"; \
			echo "Migrated: removed legacy $$legacy"; \
		fi; \
	done
	@for skill in $(SKILLS); do \
		ln -sfn "$(CURDIR)/$(SKILLS_SRC)/$$skill" "$(SKILLS_DIR)/$$skill"; \
		echo "Linked: $$skill -> $(SKILLS_DIR)/$$skill"; \
	done
	@echo "Done. $(words $(SKILLS)) skill(s) installed."

uninstall: verify-skills
	@for skill in $(SKILLS); do \
		if [ -L "$(SKILLS_DIR)/$$skill" ]; then \
			rm "$(SKILLS_DIR)/$$skill"; \
			echo "Unlinked: $$skill"; \
		fi; \
	done
	@echo "Done."

list:
	@for skill in $(ALL_SKILLS); do \
		if [ -L "$(SKILLS_DIR)/$$skill" ]; then \
			echo "[installed] $$skill"; \
		else \
			echo "[missing]   $$skill"; \
		fi; \
	done

check: verify-skills
	@ok=true; \
	for skill in $(SKILLS); do \
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

# Validate the marketplace/plugin manifests and every SKILL.md frontmatter name.
validate:
	@python3 -c "import json; json.load(open('.claude-plugin/marketplace.json'))" \
		&& echo "OK:      .claude-plugin/marketplace.json"
	@python3 -c "import json; json.load(open('$(PLUGIN_DIR)/.claude-plugin/plugin.json'))" \
		&& echo "OK:      $(PLUGIN_DIR)/.claude-plugin/plugin.json"
	@ok=true; \
	for skill in $(ALL_SKILLS); do \
		got=$$(sed -n 's/^name: *//p' "$(SKILLS_SRC)/$$skill/SKILL.md" | head -1); \
		if [ "$$got" != "$$skill" ]; then \
			echo "BAD:     $(SKILLS_SRC)/$$skill/SKILL.md has 'name: $$got', expected 'name: $$skill'"; \
			ok=false; \
		else \
			echo "OK:      $$skill -> /b:$$skill"; \
		fi; \
	done; \
	$$ok || { echo "Frontmatter name must match the skill directory so it invokes as /b:<name>."; exit 1; }
