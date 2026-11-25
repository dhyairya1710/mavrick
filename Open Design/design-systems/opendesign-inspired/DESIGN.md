# Design System Inspired by OpenDesign

> Category: Developer Tools & Creative AI  
> A tactile, warm-editorial design system featuring warm linen backgrounds, Albert Sans typography, crisp bento cards, and high-energy electric lime accents.

## Color Palette
- `--bg` (`#efe7d2`): Warm linen / paper canvas background. Provides a tactile, organic, and calm foundation.
- `--surface` (`#ffffff`): Crisp white card surfaces that pop distinctly from the warm background.
- `--surface-warm` (`#f5f5f5`): Secondary interactive surfaces and inset code containers.
- `--fg` (`#202020`): Deep charcoal ink for high-contrast primary typography.
- `--muted` (`#595959`): Mid-tone graphite for supporting paragraphs and subheads.
- `--border` (`#d9d9d9`): Delicate hairline borders providing structure without visual clutter.
- `--accent` (`#63fe13`): High-voltage electric lime accent, used sparingly for primary actions, active pills, and live statuses.
- `--accent-on` (`#0b0605`): Dark contrast glyphs and text when sitting atop the electric lime accent.

## Typography
- **Display & Headings**: `Albert Sans`, 600/700 weight, tight line-height (`1.15`), slight negative letter-spacing (`-0.025em`).
- **Body**: `Albert Sans`, 400/500 weight, comfortable reading line-height (`1.55`).
- **Monospace**: `JetBrains Mono` / `SF Mono`, used for commands, keyboard shortcuts, code snippets, and terminal telemetry.

## Voice & Tone
- Precise, creative, and empowered. Speaks directly to creators, engineers, and power users.
- Conversational yet concise. No corporate jargon or excessive adjectives.

## Imagery & Iconography
- Geometric, clean iconography with 1.5px stroke weight (Remix Icon / Lucide style).
- Tactile bento grid cards displaying code blocks, split screens, and live command executions.

## Layout & Spacing
- 8px base spacing grid (`--space-2: 8px`, `--space-4: 16px`, `--space-6: 24px`, `--space-8: 32px`).
- Generous container widths (`--container-max: 1200px`) with balanced whitespace.
- Rounded corners: `8px` for buttons, `14px`–`18px` for cards, and `9999px` for status badges.

## Motion
- Micro-interactions: 150ms ease-out transitions on buttons, interactive chips, and bento cards.
- Subtle elevation on card hover: `transform: translateY(-2px)` with soft shadows.

## Components
- **Primary Button**: Height 42px, padding 0 20px, font weight 600, pill radius, background `--accent` (`#63fe13`), color `#0b0605`.
- **Secondary Button**: Height 42px, padding 0 18px, font weight 500, pill radius, background `--surface` (`#ffffff`), border 1px solid `--border`, color `--fg`.
- **Badge / Pill**: Height 26px, padding 0 10px, font size 12px, font weight 600, pill radius (`9999px`), subtle border.
- **Card**: White background (`#ffffff`), 1px solid `--border`, 16px border-radius, 24px internal padding.

## Agent Prompt Guide
When building designs with this system, embrace the warm linen canvas (`#efe7d2`), crisp white cards (`#ffffff`), sharp `Albert Sans` typography, and intentional pops of electric lime (`#63fe13`). Keep the layout structured in an editorial bento grid with high readability and modern craftsmanship.
