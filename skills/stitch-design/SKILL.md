# Stitch Design Skill

Use this skill for frontend redesign or new UI creation using Google Stitch as the design generation tool. Stitch creates high-fidelity screens from text prompts and exports design systems as DESIGN.md files that can be applied to React/Tailwind codebases.

## Prerequisites

- Stitch MCP server connected (check `/mcp` — should show `stitch` as connected)
- Stitch API key configured in `~/.claude.json` under `mcpServers.stitch`
- Custom MCP server built with `@google/stitch-sdk` at `~/.claude/mcp-servers/stitch-mcp/`

## Available MCP Tools

| Tool | Purpose |
|------|---------|
| `stitch_list_projects` | List all Stitch projects |
| `stitch_list_screens` | List screens in a project |
| `stitch_get_screen` | Get screen metadata, HTML URL, screenshot URL |
| `stitch_generate_screen_from_text` | Generate a new screen from a text prompt |
| `stitch_edit_screens` | Edit an existing screen with follow-up prompts |
| `stitch_generate_variants` | Generate design variations (layout, color, font) |
| `stitch_create_project` | Create a new Stitch project |
| `stitch_get_project` | Get project details |

## Workflow

### Phase 1: Design Generation

**Step 1: Create a Stitch project**
```
stitch_create_project(title="Project Name")
```

**Step 2: Generate the primary screen (sets the design language)**

Use the **Zoom-Out-Zoom-In** prompting framework:
1. **Context** (zoom out): What is the app? Who uses it? What problem does it solve?
2. **Layout** (zoom in): Specific UI elements — sidebar, header, cards, grids, forms
3. **Design direction**: Adjectives, font preferences, color mood, reference aesthetic

Example prompt structure:
```
"Design a desktop web [page type] for [app name], a [description].

Target user: [persona].

Layout:
- [Component 1 with specifics]
- [Component 2 with specifics]
- [Component 3 with specifics]

Design direction: [adjectives]. [Font preferences]. [Color mood].
[Reference aesthetic — what it should feel like]."
```

Always use:
- `deviceType: "DESKTOP"` for web apps
- `modelId: "GEMINI_3_1_PRO"` for highest quality

**Step 3: Review the auto-generated design system**

Stitch creates a named design system with:
- Color palette (named tokens with hex values)
- Typography (font families, scales, weights)
- Component rules (buttons, cards, inputs, badges)
- Do's and Don'ts

Check the `designMd` field in the response — this IS your DESIGN.md.

**Step 4: Refine with targeted edits**

One or two changes per prompt. Don't mix layout + component changes.

```
stitch_edit_screens(projectId, screenId, prompt="Change the primary color from blue to coral")
```

**Step 5: Generate remaining screens**

Reference the established design system in each prompt. Stitch maintains consistency within a project.

### Phase 2: Export & Apply

**Step 6: Save DESIGN.md to project root**

Extract the `designMd` content from any screen response and save as `DESIGN.md`. Add a screen reference table mapping screen names to Stitch IDs.

**Step 7: Map design tokens to Tailwind**

Update `tailwind.config.js` with the color tokens from DESIGN.md. Key mappings:
- `surface.*` → background colors
- `on-surface.*` → text colors
- `primary.*` → accent/CTA colors
- `secondary.*` → secondary accent (waveforms, focus states)
- `tertiary.*` → neutral accents (tags, chips)
- `outline.*` → border colors (use sparingly per No-Line Rule)
- Add the heading font to `fontFamily.editorial`

**Step 8: Update global CSS**

Update component classes (`.btn-primary`, `.card`, `.input-primary`) to follow DESIGN.md rules.

**Step 9: Apply page-by-page**

For each page:
1. Optionally pull HTML via `stitch_get_screen` for pixel-level reference
2. Update Tailwind classes to match design tokens
3. Keep all React logic untouched — only change styling
4. Follow DESIGN.md component rules (ghost borders, tonal layering, etc.)

**Step 10: Extract reusable components**

Look for repeated patterns in the Stitch designs and extract into shared components:
- StatCard, StatusBadge, TagBadge, Modal, etc.

### Phase 3: Validate

- Docker build to verify compilation
- Visual check in browser
- Playwright E2E tests still pass
- Backend tests still pass

## Prompting Best Practices

### Do
- Be specific upfront — detailed first prompts save correction rounds
- Use UI/UX keywords: "navigation bar", "call-to-action", "card layout", "progress bar"
- Reference elements specifically: "primary button on sign-up form"
- Save/export after every good iteration
- Break complex layouts into sequential, focused prompts

### Don't
- Don't mix layout changes and component changes in one prompt
- Don't combine multiple features in one request
- Don't use overly long prompts (avoid 5,000+ characters)
- Don't expect Stitch to remember previous designs precisely — be incremental

### Prompt Templates

**Dashboard:**
```
"Design a desktop dashboard for [app]. Layout: Left sidebar (dark, fixed) with [nav items].
Top section: [N] stat cards. Main content: [grid/list] of [items] with [card details].
Design direction: [adjectives], [fonts], [colors]."
```

**Auth page:**
```
"Login page for [app]. Centered card on [background]. Logo + tagline. [Fields] with
[input style]. [Button style]. Links to [other pages]. [Aesthetic feel]."
```

**Detail page:**
```
"Detail page for [item] in [app]. Header: [title style], [metadata]. [N]-column layout:
[column 1 content], [column 2 content]. [Reading experience description]."
```

**Settings:**
```
"Settings page for [app]. [N] card sections: [Section 1 with fields], [Section 2],
[Section 3]. [Input style]. [Button styles]. [Spacing/layout feel]."
```

## Common Gotchas

1. **Stitch doesn't persist memory well** — each prompt should be self-contained with enough context
2. **design.md gets ~80% fidelity** when applied without HTML reference; using `stitch_get_screen` HTML gets ~95%
3. **Dark mode first** — if your design system is dark, make dark the base and remove `dark:` prefixes
4. **The No-Line Rule** — Stitch designs avoid 1px borders; use tonal layering (background color shifts) instead
5. **Font loading** — add Google Fonts import to CSS when using fonts like Newsreader
6. **MCP timeout** — screen generation takes 30-60s; don't retry if it seems slow

## Reference Implementation

See the Clio project for a complete example:
- DESIGN.md: `/Users/chadsimon/code/personal/clio/DESIGN.md`
- Tailwind config: `/Users/chadsimon/code/personal/clio/frontend/tailwind.config.js`
- Stitch Project ID: `8840700831410871660`
- 5 screens: Dashboard, Recording, Note Detail, Login, Settings
