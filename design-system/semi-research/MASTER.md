# Semi Research UI

Audience: semiconductor research analysts using conversations, source evidence, and PDF ingestion daily.
Stack: existing FastAPI, native HTML/CSS/JavaScript; no framework migration or runtime CDN.

Applied ui-ux-pro-max guidance: consistent grid, content hierarchy, native controls, keyboard focus,
reflow, readable line lengths, reduced motion, and state feedback. The generator's marketing/portfolio
patterns did not fit this operational tool and were not adopted. Its typography recommendations
were Latin-specific; retain a local/system Chinese sans-serif stack.

## Layout

- Research: 248px navigation/history sidebar, flexible conversation column, 360px evidence column.
- Conversation: compact top bar; independently scrolling messages; composer anchored below messages.
- Sources: a dedicated pane with per-turn evidence, document metadata, quotes and original media.
- Library: same navigation and tokens, upload area, real corpus counters and scan-friendly task table.
- PDF review: framed document preview, page toolbar, separate editable text and fixed action footer.
- Mobile: history disclosure above workspace; linear conversation, composer and evidence; no hidden primary actions.

## Tokens

White surfaces, neutral gray canvas, graphite text, teal primary commands, blue source links,
amber pending states and red errors. No marketing hero, decorative gradients or oversized type.
Font stack: Inter (if installed), system-ui, Noto Sans CJK SC, Microsoft YaHei, sans-serif.
Headings 18/22/28px, body 14/15/16px, metadata 12px; zero letter spacing; tabular numerals.
Spacing 4/8/12/16/24/32px. Radius 6px controls, 8px individual repeated items and modals.
Lucide icons vendored locally from lucide-static; consistent 18px with labeled icon buttons.
Hover/focus transitions 160ms; reduced-motion disables transitions.

## Validation

Check empty and populated conversations, historic evidence switching, session management,
upload and review, document browsing, and 375/768/1024/1440px viewports. Preserve accessible names,
focus visibility, native radio modes, upload input, form labels, and live request statuses.
