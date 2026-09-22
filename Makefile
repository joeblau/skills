AGENT ?= claude
# Optional explicit destination for a single agent.
SKILLS_DIR ?=
PLUGIN_DIR := b
SKILLS_SRC := $(PLUGIN_DIR)/skills
ALL_SKILLS := $(shell find $(SKILLS_SRC) -maxdepth 2 -name SKILL.md | sed 's|/SKILL.md||; s|^$(SKILLS_SRC)/||' | sort)

# Override to act on a subset: `make install SKILLS=cpr` or `make install SKILLS="cpr x-post"`
SKILLS ?= $(ALL_SKILLS)

INVALID := $(filter-out $(ALL_SKILLS),$(SKILLS))

.PHONY: install install-all uninstall list check skills verify-skills validate

verify-skills:
	@if [ -n "$(INVALID)" ]; then \
		echo "Unknown skill(s): $(INVALID)"; \
		echo "Available: $(ALL_SKILLS)"; \
		exit 1; \
	fi

skills:
	@for skill in $(ALL_SKILLS); do echo "$$skill"; done

install uninstall list check: verify-skills
	@bash scripts/skills.sh $@ --agent "$(AGENT)" $(if $(SKILLS_DIR),--directory "$(SKILLS_DIR)") $(SKILLS)

install-all:
	@$(MAKE) install AGENT=all

# Validate the marketplace/plugin manifests and every SKILL.md frontmatter name.
validate:
	@jq empty .claude-plugin/marketplace.json \
		&& echo "OK:      .claude-plugin/marketplace.json"
	@jq empty $(PLUGIN_DIR)/.claude-plugin/plugin.json \
		&& echo "OK:      $(PLUGIN_DIR)/.claude-plugin/plugin.json"
	@ok=true; \
	for skill in $(ALL_SKILLS); do \
		got=$$(sed -n 's/^name: *//p' "$(SKILLS_SRC)/$$skill/SKILL.md" | head -1); \
		if [ "$$got" != "$$skill" ]; then \
			echo "BAD:     $(SKILLS_SRC)/$$skill/SKILL.md has 'name: $$got', expected 'name: $$skill'"; \
			ok=false; \
		else \
			echo "OK:      $$skill"; \
		fi; \
	done; \
	$$ok || { echo "Frontmatter name must match the skill directory."; exit 1; }
