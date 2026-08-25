#!/usr/bin/env python3
"""Build the standalone job application dashboard from jobs/tracker.json.

Branding (name, initials, city label, resume file name, storage key) comes from
`config/user-profile.json`; see `config/user-profile.example.json`.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from user_config import STATUSES, TIERS, load_profile, resume_pdf_name

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "jobs" / "tracker.json"
OUTPUT = ROOT / "dashboard.html"

PROFILE = load_profile()

TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="theme-color" content="#f2f4f6">
<title>__BRAND_NAME__ · Application Dashboard</title>
<style>
:root {
  --bg: #f2f4f6;
  --surface: #ffffff;
  --surface-raised: rgba(255,255,255,.82);
  --text: #191f28;
  --text-secondary: #4e5968;
  --text-tertiary: #8b95a1;
  --line: rgba(0,27,55,.09);
  --blue: #3182f6;
  --blue-strong: #1b64da;
  --blue-soft: #e8f3ff;
  --green: #0ca678;
  --green-soft: #e8f8f2;
  --orange: #f08c00;
  --orange-soft: #fff4e6;
  --red: #e03131;
  --red-soft: #fff0f0;
  --purple: #7048e8;
  --purple-soft: #f3f0ff;
  --shadow-card: 0 1px 2px rgba(0,27,55,.04), 0 12px 34px rgba(0,27,55,.06);
  --shadow-float: 0 24px 80px rgba(0,27,55,.18), 0 4px 16px rgba(0,27,55,.08);
  --ease-out: cubic-bezier(.23,1,.32,1);
  --ease-in-out: cubic-bezier(.77,0,.175,1);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.55 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Pretendard", "Segoe UI", sans-serif;
  letter-spacing: -.01em;
  -webkit-font-smoothing: antialiased;
}
button, input, select { font: inherit; }
button, a { -webkit-tap-highlight-color: transparent; }
a { color: inherit; text-decoration: none; }
button { color: inherit; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {
  outline: 3px solid rgba(49,130,246,.28);
  outline-offset: 2px;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  border-bottom: 1px solid rgba(0,27,55,.07);
  background: rgba(242,244,246,.82);
  -webkit-backdrop-filter: blur(22px) saturate(180%);
  backdrop-filter: blur(22px) saturate(180%);
}
.topbar-inner {
  width: min(1180px, calc(100% - 40px));
  height: 64px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-mark {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: var(--text);
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: -.04em;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.14);
}
.brand-name { font-size: 15px; font-weight: 760; letter-spacing: -.025em; }
.brand-meta { color: var(--text-tertiary); font-size: 12px; }
.mode-label { display: inline-flex; align-items: center; min-height: 26px; padding: 0 9px; border-radius: 999px; background: var(--orange-soft); color: #9a5a00; font-size: 11px; font-weight: 760; }
.mode-label.connected { background: var(--green-soft); color: var(--green); }
.top-actions { display: flex; align-items: center; gap: 6px; }
.icon-button, .quiet-button, .primary-button, .secondary-button, .link-button {
  border: 0;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  white-space: nowrap;
  font-weight: 680;
  transition: transform 140ms var(--ease-out), background-color 160ms ease, color 160ms ease, box-shadow 160ms ease;
}
.icon-button:active, .quiet-button:active, .primary-button:active, .secondary-button:active, .link-button:active { transform: scale(.97); }
.icon-button {
  width: 36px;
  height: 36px;
  border-radius: 12px;
  background: transparent;
  color: var(--text-secondary);
}
.icon-button:hover { background: rgba(0,27,55,.06); color: var(--text); }
.quiet-button { height: 36px; padding: 0 12px; border-radius: 12px; background: transparent; color: var(--text-secondary); font-size: 13px; }
.quiet-button:hover { background: rgba(0,27,55,.06); color: var(--text); }
.primary-button { min-height: 44px; padding: 0 17px; border-radius: 14px; background: var(--blue); color: #fff; box-shadow: 0 7px 18px rgba(49,130,246,.22); }
.primary-button:hover { background: var(--blue-strong); box-shadow: 0 9px 22px rgba(49,130,246,.28); }
.secondary-button { min-height: 42px; padding: 0 15px; border-radius: 13px; background: rgba(255,255,255,.14); color: #fff; box-shadow: inset 0 0 0 1px rgba(255,255,255,.24); }
.secondary-button:hover { background: rgba(255,255,255,.21); }
.link-button { min-height: 36px; padding: 0 12px; border-radius: 11px; background: var(--blue-soft); color: var(--blue-strong); font-size: 13px; }
.link-button:hover { background: #dbeeff; }
svg { width: 18px; height: 18px; flex: 0 0 auto; }
.shell { width: min(1180px, calc(100% - 40px)); margin: 0 auto; padding: 58px 0 80px; }
.intro { max-width: 720px; margin-bottom: 26px; }
.eyebrow { margin: 0 0 8px; color: var(--blue-strong); font-size: 13px; font-weight: 760; }
h1 { margin: 0; font-size: clamp(34px, 5vw, 52px); line-height: 1.08; letter-spacing: -.045em; font-weight: 790; }
.intro-copy { margin: 14px 0 0; color: var(--text-secondary); font-size: 17px; line-height: 1.55; }
.spotlight {
  position: relative;
  overflow: hidden;
  min-height: 258px;
  border-radius: 28px;
  padding: 30px;
  color: #fff;
  background: linear-gradient(135deg, #1b64da 0%, #3182f6 58%, #55a3ff 100%);
  box-shadow: 0 18px 45px rgba(49,130,246,.24);
}
.spotlight::after {
  content: "";
  position: absolute;
  width: 420px;
  height: 420px;
  right: -155px;
  top: -260px;
  border-radius: 50%;
  background: rgba(255,255,255,.15);
  box-shadow: 0 0 80px rgba(255,255,255,.12);
  pointer-events: none;
}
.spotlight-grid { position: relative; z-index: 1; display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 28px; align-items: end; height: 100%; }
.spotlight-kicker { display: flex; align-items: center; gap: 8px; margin-bottom: 22px; color: rgba(255,255,255,.78); font-size: 13px; font-weight: 700; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: #9af3d4; box-shadow: 0 0 0 5px rgba(154,243,212,.13); }
.spotlight-company { color: rgba(255,255,255,.78); font-size: 15px; font-weight: 700; }
.spotlight h2 { margin: 2px 0 10px; max-width: 760px; font-size: clamp(27px,4vw,40px); line-height: 1.13; letter-spacing: -.04em; }
.spotlight-reason { max-width: 740px; margin: 0; color: rgba(255,255,255,.82); font-size: 15px; line-height: 1.55; }
.spotlight-meta { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 18px; }
.glass-chip { border: 1px solid rgba(255,255,255,.22); border-radius: 999px; padding: 5px 9px; background: rgba(255,255,255,.11); color: rgba(255,255,255,.9); font-size: 12px; font-weight: 650; }
.spotlight-actions { display: flex; flex-direction: column; gap: 9px; min-width: 148px; }
.spotlight-actions .primary-button { background: #fff; color: var(--blue-strong); box-shadow: 0 8px 24px rgba(0,42,105,.18); }
.spotlight-actions .primary-button:hover { background: #f7fbff; }
.metrics { display: grid; grid-template-columns: repeat(5,1fr); gap: 22px; margin: 18px 0 42px; }
.metric {
  position: relative;
  min-height: 108px;
  padding: 20px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: var(--surface-raised);
  box-shadow: var(--shadow-card);
}
.metric:not(:last-child)::after {
  content: '\2192';
  position: absolute;
  right: -19px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-tertiary);
  font-size: 15px;
  font-weight: 700;
}
.metric-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.metric-value { font-size: 28px; line-height: 1; font-weight: 780; letter-spacing: -.045em; }
.metric-icon { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 12px; background: var(--blue-soft); color: var(--blue-strong); }
.metric-icon.green { background: var(--green-soft); color: var(--green); }
.metric-icon.purple { background: var(--purple-soft); color: var(--purple); }
.metric-icon.orange { background: var(--orange-soft); color: var(--orange); }
.metric-label { margin-top: 12px; color: var(--text-secondary); font-size: 13px; font-weight: 650; }
.section { margin-top: 28px; }
.section-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; margin-bottom: 16px; }
.section-heading h2 { margin: 0; font-size: 25px; line-height: 1.2; letter-spacing: -.035em; }
.section-heading p { margin: 6px 0 0; color: var(--text-tertiary); font-size: 14px; }
.filter-panel {
  position: sticky;
  top: 76px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: rgba(255,255,255,.82);
  -webkit-backdrop-filter: blur(22px) saturate(180%);
  backdrop-filter: blur(22px) saturate(180%);
  box-shadow: 0 8px 28px rgba(0,27,55,.06);
}
.search-wrap { position: relative; flex: 1 1 260px; min-width: 180px; }
.search-wrap svg { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--text-tertiary); pointer-events: none; }
.search-wrap input {
  width: 100%;
  height: 42px;
  border: 0;
  border-radius: 13px;
  padding: 0 12px 0 40px;
  background: var(--bg);
  color: var(--text);
  outline: 0;
}
.search-wrap input:focus { background: #fff; box-shadow: inset 0 0 0 2px rgba(49,130,246,.35); }
.segmented { display: flex; align-items: center; gap: 3px; margin: 0; padding: 3px; border: 0; border-radius: 13px; background: var(--bg); }
.segment-button { height: 36px; border: 0; border-radius: 10px; padding: 0 11px; background: transparent; color: var(--text-tertiary); cursor: pointer; font-size: 12px; font-weight: 700; transition: background-color 150ms ease, color 150ms ease, box-shadow 150ms ease, transform 120ms var(--ease-out); }
.segment-button:active { transform: scale(.97); }
.segment-button.active { background: #fff; color: var(--text); box-shadow: 0 2px 8px rgba(0,27,55,.08); }
.filter-select { height: 42px; border: 0; border-radius: 13px; padding: 0 34px 0 12px; background-color: var(--bg); color: var(--text-secondary); font-size: 12px; font-weight: 650; cursor: pointer; }
.role-list { display: grid; gap: 9px; }
.role-row {
  display: grid;
  grid-template-columns: 64px minmax(260px,1.45fr) minmax(170px,.85fr) 150px auto;
  align-items: center;
  gap: 16px;
  min-height: 112px;
  padding: 18px 18px 18px 14px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: var(--surface);
  box-shadow: 0 1px 2px rgba(0,27,55,.03);
  transition: transform 180ms var(--ease-out), box-shadow 180ms ease, border-color 180ms ease;
}
.role-row:hover { transform: translateY(-1px); border-color: rgba(49,130,246,.18); box-shadow: 0 11px 30px rgba(0,27,55,.075); }
.score-ring {
  width: 52px;
  height: 52px;
  display: grid;
  place-items: center;
  border-radius: 18px;
  background: var(--blue-soft);
  color: var(--blue-strong);
  font-size: 17px;
  font-weight: 790;
  letter-spacing: -.03em;
}
.score-ring.high { background: var(--green-soft); color: var(--green); }
.score-ring.low { background: var(--orange-soft); color: var(--orange); }
.role-company { color: var(--text-secondary); font-size: 13px; font-weight: 700; }
.role-title { margin-top: 1px; font-size: 17px; line-height: 1.3; font-weight: 760; letter-spacing: -.022em; }
.role-match { margin-top: 7px; overflow: hidden; color: var(--text-tertiary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.meta-stack { min-width: 0; }
.meta-primary { font-size: 13px; font-weight: 680; }
.meta-secondary { margin-top: 4px; overflow: hidden; color: var(--text-tertiary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.status-select { width: 100%; height: 40px; border: 1px solid var(--line); border-radius: 12px; padding: 0 32px 0 11px; background-color: #fff; color: var(--text-secondary); font-size: 12px; font-weight: 700; cursor: pointer; }
.row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
.row-action { width: 38px; height: 38px; display: grid; place-items: center; border: 0; border-radius: 12px; background: var(--bg); color: var(--text-secondary); cursor: pointer; transition: transform 120ms var(--ease-out), background-color 150ms ease, color 150ms ease; }
a.row-action { display: grid; }
.row-action:hover { background: var(--blue-soft); color: var(--blue-strong); }
.row-action:active { transform: scale(.95); }
.queue-button { min-height: 38px; padding: 0 11px; border: 0; border-radius: 12px; background: var(--blue-soft); color: var(--blue-strong); cursor: pointer; font-size: 12px; font-weight: 740; }
.queue-button:hover { background: #dbeeff; }
.fixture-queue { display: inline-flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.fixture-queue small { color: #4e5968; font-size: 14px; line-height: 1.4; }
.spotlight .fixture-queue small { color: rgba(255,255,255,.94); }
.storage-warning { margin: 0 20px 16px; color: #8c5700; font-size: 14px; line-height: 1.4; }
.automation-state { margin: 0 20px 16px; padding: 13px 14px; border-radius: 14px; background: var(--orange-soft); color: #8c5700; font-size: 14px; font-weight: 650; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.queue-button:disabled { cursor: wait; opacity: .65; }
.automation-state.connected { background: var(--green-soft); color: #087657; }
.automation-state strong { color: inherit; }
.empty { display: none; padding: 72px 20px; text-align: center; color: var(--text-tertiary); }
.empty strong { display: block; margin-bottom: 5px; color: var(--text-secondary); font-size: 17px; }
.automation {
  margin-top: 42px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: var(--surface);
  overflow: hidden;
}
.automation summary { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 19px 20px; cursor: pointer; list-style: none; font-weight: 740; }
.automation summary::-webkit-details-marker { display: none; }
.automation-summary-copy { display: flex; align-items: center; gap: 12px; }
.automation-summary-copy > span:first-child { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 13px; background: var(--green-soft); color: var(--green); }
.automation-subtitle { margin-top: 2px; color: var(--text-tertiary); font-size: 12px; font-weight: 500; }
.chevron { color: var(--text-tertiary); transition: transform 180ms var(--ease-out); }
.automation[open] .chevron { transform: rotate(180deg); }
.automation-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 10px; padding: 0 20px 20px; }
.automation-card { padding: 15px; border-radius: 15px; background: var(--bg); }
.automation-card h3 { margin: 0 0 6px; font-size: 14px; }
.automation-card p { margin: 0; color: var(--text-tertiary); font-size: 12px; }
.auto-state { display: inline-block; margin-top: 10px; color: var(--orange); font-size: 11px; font-weight: 760; }
.auto-state.ready { color: var(--green); }
footer { padding: 30px 0 0; text-align: center; color: var(--text-tertiary); font-size: 11px; }
dialog {
  width: min(640px, calc(100% - 28px));
  max-height: min(820px, calc(100vh - 28px));
  margin: auto;
  padding: 0;
  border: 0;
  border-radius: 26px;
  background: transparent;
  color: var(--text);
  box-shadow: var(--shadow-float);
  overflow: hidden;
}
dialog::backdrop { background: rgba(11,18,27,.34); -webkit-backdrop-filter: blur(3px); backdrop-filter: blur(3px); }
.sheet { max-height: inherit; overflow: auto; background: rgba(255,255,255,.96); -webkit-backdrop-filter: blur(28px) saturate(180%); backdrop-filter: blur(28px) saturate(180%); }
.sheet-head { position: sticky; top: 0; z-index: 2; display: flex; justify-content: space-between; gap: 16px; padding: 22px 24px 15px; background: rgba(255,255,255,.9); -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px); }
.sheet-kicker { color: var(--text-tertiary); font-size: 12px; font-weight: 700; }
.sheet-title { margin: 3px 0 0; font-size: 25px; line-height: 1.2; letter-spacing: -.035em; }
.sheet-body { padding: 4px 24px 26px; }
.sheet-score { display: inline-flex; align-items: baseline; gap: 4px; margin: 6px 0 18px; color: var(--green); font-weight: 780; }
.sheet-score strong { font-size: 30px; letter-spacing: -.04em; }
.detail-grid { display: grid; grid-template-columns: repeat(2,1fr); gap: 9px; margin-bottom: 20px; }
.detail-cell { padding: 13px 14px; border-radius: 14px; background: var(--bg); }
.detail-label { color: var(--text-tertiary); font-size: 11px; font-weight: 700; }
.detail-value { margin-top: 4px; font-size: 13px; font-weight: 650; }
.detail-section { padding: 18px 0; border-top: 1px solid var(--line); }
.detail-section h3 { margin: 0 0 8px; font-size: 14px; }
.detail-section p { margin: 0; color: var(--text-secondary); font-size: 14px; }
.keyword-list { display: flex; flex-wrap: wrap; gap: 6px; }
.keyword { padding: 5px 8px; border-radius: 9px; background: var(--bg); color: var(--text-secondary); font-size: 11px; font-weight: 650; }
.sheet-actions { position: sticky; bottom: 0; display: flex; gap: 8px; padding: 14px 24px calc(14px + env(safe-area-inset-bottom)); border-top: 1px solid var(--line); background: rgba(255,255,255,.9); -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px); }
.sheet-actions .primary-button { flex: 1; }
.sheet-actions .link-button { min-height: 44px; }
.toast {
  position: fixed;
  left: 50%;
  bottom: 24px;
  z-index: 60;
  max-width: calc(100% - 32px);
  padding: 11px 15px;
  border-radius: 13px;
  background: rgba(25,31,40,.92);
  color: #fff;
  box-shadow: 0 14px 40px rgba(0,0,0,.2);
  opacity: 0;
  transform: translate(-50%, 10px) scale(.97);
  pointer-events: none;
  transition: opacity 180ms var(--ease-out), transform 180ms var(--ease-out);
  font-size: 13px;
  font-weight: 650;
}
.toast.show { opacity: 1; transform: translate(-50%,0) scale(1); }
.mobile-only { display: none; }
@media (max-width: 980px) {
  .metrics { grid-template-columns: repeat(2,1fr); gap: 12px; }
  .metric:not(:last-child)::after { content: none; }
  .filter-panel { flex-wrap: wrap; }
  .search-wrap { flex-basis: 100%; }
  .role-row { grid-template-columns: 56px minmax(240px,1fr) 145px auto; }
  .role-row .meta-stack { display: none; }
  .automation-grid { grid-template-columns: repeat(2,1fr); }
}
@media (max-width: 700px) {
  .topbar-inner, .shell { width: min(100% - 28px, 1180px); }
  .topbar-inner { height: 58px; }
  .brand-meta, .desktop-only { display: none; }
  .mobile-only { display: inline-flex; }
  .shell { padding-top: 36px; }
  h1 { font-size: 36px; }
  .intro-copy { font-size: 15px; }
  .spotlight { min-height: 0; padding: 24px; border-radius: 24px; }
  .spotlight-grid { grid-template-columns: 1fr; }
  .spotlight-actions { flex-direction: row; flex-wrap: wrap; min-width: 0; }
  .spotlight-actions > * { flex: 1 1 calc(50% - 5px); }
  .metrics { gap: 8px; margin-bottom: 34px; }
  .metric { min-height: 96px; padding: 16px; border-radius: 17px; }
  .metric-value { font-size: 24px; }
  .metric-icon { width: 30px; height: 30px; border-radius: 10px; }
  .section-heading { align-items: flex-start; }
  .filter-panel { top: 67px; padding: 8px; border-radius: 16px; }
  .segmented { width: 100%; overflow-x: auto; }
  .segment-button { flex: 1; }
  .filter-select { flex: 1; min-width: 0; }
  .role-row { grid-template-columns: 48px minmax(0,1fr) auto; gap: 11px; min-height: 0; padding: 15px 12px; border-radius: 18px; }
  .score-ring { width: 44px; height: 44px; border-radius: 15px; font-size: 15px; }
  .role-title { font-size: 15px; }
  .role-match { max-width: 100%; }
  .role-row .status-select { grid-column: 2 / 4; grid-row: 2; }
  .row-actions { grid-column: 3; grid-row: 1; }
  .row-actions .material-link { display: none; }
  .automation-grid { grid-template-columns: 1fr; }
  dialog { align-self: end; width: 100%; max-width: none; max-height: 88vh; margin: auto 0 0; border-radius: 26px 26px 0 0; }
  .detail-grid { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
  .role-row:hover { transform: none; }
}
@media (prefers-reduced-transparency: reduce) {
  .topbar, .filter-panel, .sheet, .sheet-head, .sheet-actions { background: #fff; -webkit-backdrop-filter: none; backdrop-filter: none; }
}
@media (prefers-contrast: more) {
  :root { --line: rgba(0,0,0,.26); --text-secondary: #32373d; --text-tertiary: #5f6872; }
}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">__BRAND_INITIALS__</div>
      <div>
        <div class="brand-name">Applications</div>
        <div class="brand-meta">__BRAND_CITY__ · __BRAND_NAME__</div>
        <span class="mode-label" id="modeLabel" role="status" aria-live="polite" aria-atomic="true">Local scratch · offline</span>
      </div>
    </div>
    <nav class="top-actions" aria-label="Dashboard tools">
      <a class="quiet-button desktop-only" href="jobs/tracker.json">Source data</a>
      <a class="quiet-button desktop-only" href="applications/_master/resume.md">Master resume</a>
      <button class="icon-button" id="exportBtn" type="button" aria-label="Export application statuses" title="Export application statuses">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M5 16v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3"/></svg>
      </button>
      <button class="icon-button" id="resetBtn" type="button" aria-label="Reset browser state" title="Reset browser state">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>
      </button>
    </nav>
  </div>
</header>
<main class="shell">
  <section class="intro" aria-labelledby="page-title">
    <p class="eyebrow">Today's application plan</p>
    <h1 id="page-title">Focus on the<br>next application.</h1>
    <p class="intro-copy" id="introCopy"></p>
  </section>

  <section class="spotlight" id="spotlight" aria-label="Top posting to apply to first"></section>
  <section class="metrics" id="metrics" aria-label="Application summary"></section>

  <section class="section" aria-labelledby="pipeline-title">
    <div class="section-heading">
      <div>
        <h2 id="pipeline-title">Application pipeline</h2>
        <p>Showing <span id="visibleCount"></span> postings. <span id="statusStorageCopy">Status changes are saved right in this browser.</span></p>
      </div>
    </div>
    <div class="filter-panel" aria-label="Posting filters">
      <label class="search-wrap">
        <span class="sr-only"></span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.7-3.7"/></svg>
        <input type="search" id="search" aria-label="Search company, role, or skill" placeholder="Search company, role, or skill">
      </label>
      <fieldset class="segmented" id="locationSegments">
        <legend class="sr-only">Work model</legend>
        <button class="segment-button active" type="button" data-location="all" aria-pressed="true">All</button>
        <button class="segment-button" type="button" data-location="local" aria-pressed="false">__BRAND_CITY__</button>
        <button class="segment-button" type="button" data-location="remote_bonus" aria-pressed="false">Remote</button>
        <button class="segment-button" type="button" data-location="relocation" aria-pressed="false">Relocation</button>
      </fieldset>
      <select class="filter-select" id="tierFilter" aria-label="Priority">
        <option value="all">All priorities</option>__TIER_FILTER_OPTIONS__
      </select>
      <select class="filter-select" id="statusFilter" aria-label="Application status">
        <option value="all">All statuses</option>__STATUS_FILTER_OPTIONS__
      </select>
    </div>
    <div class="role-list" id="roleList"></div>
    <div class="empty" id="empty"><strong>No postings match.</strong>Try a different search or filter.</div>
  </section>

  <details class="automation">
    <summary>
      <span class="automation-summary-copy">
        <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6"/></svg></span>
        <span class="automation-summary-text"><span>Application automation</span><span class="automation-subtitle">A connected queue pauses wherever review is needed.</span></span>
      </span>
      <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m7 9 5 5 5-5"/></svg>
    </summary>
    <p class="automation-state" id="automationState"><strong>Local scratch mode</strong> · Automation and API requests are off. Opening a posting only navigates to the external page.</p>
    <p class="storage-warning" id="storageWarning" role="status" hidden></p>
    <div class="automation-grid">
      <article class="automation-card"><h3>Queue status</h3><p id="queueStatus">Offline · no pending jobs</p><span class="auto-state" id="queueStateLabel">Local only</span></article>
      <article class="automation-card"><h3>Kill switch</h3><p id="killSwitchStatus">Automation cannot start while offline.</p><span class="auto-state" id="killSwitchLabel">Locked</span></article>
      <article class="automation-card"><h3>Today's quota</h3><p id="dailyQuota">Connect to see the daily queue limit.</p><span class="auto-state" id="quotaLabel">Offline</span></article>
      <article class="automation-card"><h3>Needs review</h3><p id="checkpointMessage">No checkpoints in this mode.</p><span class="auto-state" id="checkpointLabel">Idle</span></article>
    </div>
  </details>
  <footer>As of __GENERATED_AT__ · jobs/tracker.json · export browser state before switching browsers</footer>
</main>

<dialog id="roleDialog" aria-labelledby="dialogTitle">
  <div class="sheet">
    <div class="sheet-head">
      <div><div class="sheet-kicker" id="dialogCompany"></div><h2 class="sheet-title" id="dialogTitle"></h2></div>
      <button class="icon-button" id="closeDialog" type="button" aria-label="Close details"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
    </div>
    <div id="dialogContent"></div>
  </div>
</dialog>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script id="job-data" type="application/json">__JOB_DATA__</script>
<script>
const data = JSON.parse(document.getElementById('job-data').textContent);
const roles = data.roles;
const storageKey = '__STORAGE_KEY__';
const statusMeta = __STATUS_META__;
const tierMeta = __TIER_META__;
const statusOptions = statusMeta.map(item => item[0]);
const roleIds = new Set(roles.map(role => role.id));
let storageWarning = '';
function setStorageWarning(message) {
  storageWarning = message;
  const warning = document.getElementById('storageWarning');
  if (warning) {
    warning.hidden = !message;
    warning.textContent = message;
  }
}
function storageOperation(operation, fallback) {
  try {
    return operation();
  } catch (error) {
    setStorageWarning('Browser storage is unavailable; status changes persist only in this page. Embedded data is unchanged.');
    return fallback;
  }
}
const readStoredStatuses = () => storageOperation(() => localStorage.getItem(storageKey), null);
const writeStoredStatuses = () => storageOperation(() => localStorage.setItem(storageKey, JSON.stringify(saved)), undefined);
const removeStoredStatuses = () => storageOperation(() => localStorage.removeItem(storageKey), undefined);
function loadSavedStatuses() {
  const value = readStoredStatuses();
  if (value === null) return {};
  try {
    const parsed = JSON.parse(value);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('invalid shape');
    const entries = Object.entries(parsed);
    if (entries.some(([id, status]) => !roleIds.has(id) || !statusOptions.includes(status))) throw new Error('invalid status');
    return Object.fromEntries(entries);
  } catch (error) {
    setStorageWarning('Stored browser state was safely reset. Embedded data is unchanged.');
    removeStoredStatuses();
    return {};
  }
}
let saved = loadSavedStatuses();
let locationFilter = 'all';
let toastTimer;
let csrfToken = null;
let snapshot = null;
let connectedRoles = {};
let isConnected = false;
const staticMode = window.location.protocol === 'file:';
let pollTimer = null;
let reconnectTimer = null;
let pollInFlight = false;
let reconnectAttempt = 0;
const pollIntervalMs = 3000;
const maxReconnectDelayMs = 30000;
const queueInFlight = new Set();
const labels = {
  ...Object.fromEntries(statusMeta), ...Object.fromEntries(tierMeta),
  queued:'Queued', running:'Running', paused:'Awaiting user', awaiting_user:'Awaiting user', manual_follow_up:'Manual follow-up'
};
const icons = {
  document:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5M10 13h5M10 17h5"/></svg>',
  arrow:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"/></svg>',
  detail:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>'
};
const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const currentStatus = role => isConnected ? connectedRoles[role.id]?.status : (staticMode ? (saved[role.id] || role.status) : role.status);
const automationFor = role => connectedRoles[role.id]?.automation || {};
const isLocal = role => !['remote_bonus','relocation'].includes(role.tier);
const pathUrl = path => path.split('/').map(encodeURIComponent).join('/');
const formatDate = value => value ? new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric'}).format(new Date(value + 'T00:00:00')) : 'No date';
const labelFor = value => labels[value] || String(value ?? '').replaceAll('_',' ');
const workerState = worker => worker?.state === 'running' && worker.automatic_progress === true && worker.can_queue === true ? 'running' : worker?.state === 'manual' && worker.automatic_progress === false && worker.can_queue === true ? 'manual' : worker?.state === 'unavailable' && worker.automatic_progress === false && worker.can_queue === false ? 'unavailable' : null;
const hasQueueAuthority = () => isConnected && snapshot?.automation?.fixture_mode === true && snapshot?.automation?.kill_switch_active === false && typeof snapshot?.catalog_revision === 'string' && snapshot.catalog_revision.trim() !== '' && ['running','manual'].includes(workerState(snapshot?.worker));
const idempotencyKey = role => `dashboard-queue-${role.id}-${snapshot.catalog_revision}`;
const safeExternalUrl = value => {
  try {
    const url = new URL(value);
    return ['https:','http:'].includes(url.protocol) ? url.href : null;
  } catch (error) {
    return null;
  }
};
const safeConnectionMessage = 'Could not reach the local fixture service. Switching to read-only.';
const safeQueueMessage = 'The fixture queue request was rejected. No real application was submitted.';
const resumePdfName = __RESUME_PDF_NAME__;
const materialUrl = role => isConnected ? `/api/v1/materials/${encodeURIComponent(role.id)}` : `${pathUrl(role.application_dir)}/${encodeURIComponent(resumePdfName)}`;

function showToast(message) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 1900);
}
function setText(id, value) { document.getElementById(id).textContent = value; }
function setStatus(id, status, announce = true) {
  if (!staticMode || isConnected) return;
  const role = roles.find(item => item.id === id);
  saved[id] = status;
  writeStoredStatuses();
  renderAll();
  if (announce && role) showToast(`${role.company} status changed to '${labelFor(status)}'.`);
}
function statusSelect(role, className = 'status-select') {
  const disabled = !staticMode || isConnected ? ' disabled title="Read-only while in service mode."' : '';
  const options = statusOptions.map(status => `<option value="${status}" ${currentStatus(role) === status ? 'selected' : ''}>${labelFor(status)}</option>`).join('');
  return `<select class="${className}" data-status-id="${esc(role.id)}" aria-label="${esc(role.company)} ${esc(role.title)} application status"${disabled}>${options}</select>`;
}
function commandState(role) {
  const state = automationFor(role).state;
  return ['queued','running','awaiting_user','applied','manual_follow_up','paused'].includes(state) ? state : '';
}
function queueControl(role, className = 'queue-button') {
  if (!hasQueueAuthority() || currentStatus(role) !== 'materials_ready') return '';
  const state = commandState(role);
  if (state) return `<span class="${className}" aria-label="Automation state ${esc(labelFor(state))}">${esc(labelFor(state))}</span>`;
  return `<span class="fixture-queue"><button class="${className}" type="button" data-queue-role="${esc(role.id)}">Add to fixture queue (does not submit)</button><small>Fixture only · no real application is submitted.</small></span>`;
}
function externalApply(role, className = 'primary-button') {
  const url = safeExternalUrl(role.apply_url);
  if (!url) return `<span class="${className}" aria-disabled="true" title="No valid HTTP(S) apply link.">No apply link</span>`;
  const rowAction = className.includes('row-action');
  const label = rowAction ? icons.arrow : `Open posting ${icons.arrow}`;
  return `<a class="${className} application-link" href="${esc(url)}" target="_blank" rel="noopener" aria-label="Open ${esc(role.company)} posting page" title="Open posting page">${label}</a>`;
}
function renderAutomationSurface() {
  if (!isConnected) return;
  const active = Object.values(connectedRoles).filter(item => item.automation?.state && item.automation.state !== 'idle');
  const checkpoint = active.map(item => item.automation?.checkpoint_code).find(Boolean);
  const pause = active.map(item => item.automation?.pause).find(Boolean);
  const automation = snapshot.automation;
  const state = workerState(snapshot.worker);
  if (!state) return resetOfflineSurface();
  const workerLabel = state === 'running' ? 'Automatic' : state === 'manual' ? 'Manual' : 'Unavailable';
  document.getElementById('modeLabel').textContent = `Connected · local service · fixture · ${workerLabel}`;
  document.getElementById('modeLabel').classList.add('connected');
  document.getElementById('statusStorageCopy').textContent = state === 'running' ? 'The fixture service advances the queue automatically. Not a real application record.' : state === 'manual' ? 'The fixture service is connected but job progress is manual. Not a real application record.' : 'The fixture worker is unavailable; nothing can be queued. Not a real application record.';
  document.getElementById('automationState').classList.add('connected');
  document.getElementById('automationState').textContent = pause ? `Needs review: ${pause.stage} · ${pause.reason} · ${pause.checkpoint_id}` : 'Fixture only · nothing is submitted to real providers, and queue results are not evidence of real applications.';
  setText('queueStatus', active.length ? `${active.length} fixture job(s) · ${active.map(item => labelFor(item.automation.state)).join(', ')}` : `No pending fixture jobs · ${workerLabel}`);
  setText('queueStateLabel', hasQueueAuthority() ? `${workerLabel} ready` : 'Queue unavailable');
  setText('killSwitchStatus', automation.kill_switch_active ? 'Active · new fixture jobs are halted.' : 'Inactive · only fixture jobs are allowed.');
  setText('killSwitchLabel', automation.kill_switch_active ? 'Halted' : 'fixture');
  const quota = automation.daily_quota;
  setText('dailyQuota', quota ? `Today ${quota.used || 0} / ${quota.limit || 0}` : 'The fixture service manages the daily limit.');
  setText('quotaLabel', quota ? 'Fixture quota' : 'Fixture managed');
  setText('checkpointMessage', checkpoint ? `Review ${checkpoint} and proceed manually.` : 'No checkpoints · the fixture never submits real applications.');
  setText('checkpointLabel', checkpoint ? 'Action needed' : 'Fixture idle');
}
function resetOfflineSurface() {
  const disconnected = !staticMode;
  document.getElementById('modeLabel').textContent = disconnected ? 'Disconnected · read-only' : 'Local scratch mode';
  document.getElementById('modeLabel').classList.remove('connected');
  document.getElementById('statusStorageCopy').textContent = disconnected ? 'Reading embedded data only until the service connection recovers. Local state is not modified.' : 'Status changes are saved only in this browser and are not real application records.';
  document.getElementById('automationState').classList.remove('connected');
  document.getElementById('automationState').innerHTML = disconnected ? '<strong>Disconnected · read-only</strong> · Automation and status changes are unavailable.' : '<strong>Local scratch mode</strong> · Automation and API requests are off; status changes are not real application records.';
  setText('queueStatus', 'Offline · no pending jobs');
  setText('queueStateLabel', disconnected ? 'Read-only' : 'Local only');
  setText('killSwitchStatus', 'Automation cannot start while offline.');
  setText('killSwitchLabel', 'Locked');
  setText('dailyQuota', disconnected ? 'Read-only until the connection recovers.' : 'Quota is unavailable from a static file.');
  setText('quotaLabel', 'Offline');
  setText('checkpointMessage', 'No checkpoints in this mode.');
  setText('checkpointLabel', 'Idle');
  document.getElementById('resetBtn').hidden = disconnected;
  const warning = document.getElementById('storageWarning');
  warning.hidden = !storageWarning;
  warning.textContent = storageWarning;
}
function clearTimer(timer) {
  if (timer !== null) clearTimeout(timer);
  return null;
}
function stopPolling() {
  pollTimer = clearTimer(pollTimer);
}
function schedulePolling() {
  if (staticMode || pollTimer !== null || !isConnected) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    await loadConnectedState();
  }, pollIntervalMs);
}
function scheduleReconnect() {
  if (reconnectTimer !== null || staticMode) return;
  const delay = Math.min(1000 * (2 ** reconnectAttempt), maxReconnectDelayMs);
  reconnectAttempt = Math.min(reconnectAttempt + 1, 5);
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    await loadConnectedState();
  }, delay);
}
function isSupportedLoopbackOrigin() {
  return window.location.protocol === 'http:' && window.location.hostname === '127.0.0.1';
}
async function loadConnectedState() {
  if (staticMode || !isSupportedLoopbackOrigin() || pollInFlight) return;
  pollInFlight = true;
  try {
    const sessionResponse = await fetch('/api/v1/session', {credentials:'same-origin', cache:'no-store'});
    if (!sessionResponse.ok) throw new Error(`session (${sessionResponse.status})`);
    const session = await sessionResponse.json();
    const snapshotResponse = await fetch('/api/v1/snapshot', {credentials:'same-origin', cache:'no-store'});
    if (!snapshotResponse.ok) throw new Error(`snapshot (${snapshotResponse.status})`);
    const candidate = await snapshotResponse.json();
    const worker = candidate?.worker;
    if (candidate?.automation?.fixture_mode !== true || session?.fixture_mode !== true || typeof candidate.catalog_revision !== 'string' || candidate.catalog_revision.trim() === '' || !workerState(worker) || typeof session?.csrf_token !== 'string' || session.csrf_token === '') throw new Error('untrusted service state');
    const remoteRoles = Array.isArray(candidate.roles) ? candidate.roles : [];
    const expectedIds = new Set(roles.map(role => role.id));
    if (remoteRoles.length !== expectedIds.size || new Set(remoteRoles.map(role => role.role_id)).size !== expectedIds.size || remoteRoles.some(role => !expectedIds.has(role.role_id) || !statusOptions.includes(role.status))) {
      throw new Error('incomplete service snapshot');
    }
    snapshot = candidate;
    csrfToken = session.csrf_token;
    connectedRoles = Object.fromEntries(remoteRoles.map(item => [item.role_id, item]));
    isConnected = true;
    reconnectAttempt = 0;
    reconnectTimer = clearTimer(reconnectTimer);
    renderAll();
    renderAutomationSurface();
    schedulePolling();
  } catch (error) {
    stopPolling();
    isConnected = false;
    csrfToken = null;
    snapshot = null;
    connectedRoles = {};
    resetOfflineSurface();
    renderAll();
    showToast(safeConnectionMessage);
    scheduleReconnect();
  } finally {
    pollInFlight = false;
  }
}
async function queueApplication(id) {
  if (!hasQueueAuthority() || !csrfToken || queueInFlight.has(id)) return;
  const role = roles.find(item => item.id === id);
  if (!role || currentStatus(role) !== 'materials_ready') return;
  queueInFlight.add(id);
  const key = idempotencyKey(role);
  document.querySelectorAll(`[data-queue-role="${CSS.escape(id)}"]`).forEach(button => { button.disabled = true; button.textContent = 'Queuing…'; });
  try {
    const response = await fetch(`/api/v1/roles/${encodeURIComponent(id)}/commands`, {
      method:'POST',
      credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken,'Idempotency-Key':key},
      body:JSON.stringify({mode:'batch',idempotency_key:key})
    });
    if (!response.ok) {
      throw new Error('command rejected');
    }
    const command = await response.json();
    const queueState = command.state === 'accepted' ? 'queued' : command.state;
    connectedRoles[id] = {...(connectedRoles[id] || {}), automation:{...(automationFor(role)), state:queueState, command_id:command.id}};
    renderAll();
    renderAutomationSurface();
    showToast(`Queued a fixture job for ${role.company}. No real application was submitted.`);
  } catch (error) {
    renderAll();
    showToast(safeQueueMessage);
  } finally {
    queueInFlight.delete(id);
  }
}
function isPending(role) {
  return ['discovered', 'materials_ready'].includes(currentStatus(role));
}
function renderIntro() {
  const ready = roles.filter(isPending).length;
  document.getElementById('introCopy').textContent = `${ready} collected posting(s) are ready to apply. Finish the most promising one first.`;
}
function nextRole() {
  return roles.filter(role => currentStatus(role) === 'materials_ready' && role.tier !== 'relocation').sort((a,b) => Number(isLocal(b)) - Number(isLocal(a)) || b.score - a.score || b.posted.localeCompare(a.posted))[0];
}
function renderSpotlight() {
  const role = nextRole();
  const container = document.getElementById('spotlight');
  if (!role) {
    container.innerHTML = '<div class="spotlight-grid"><div><div class="spotlight-kicker"><span class="live-dot"></span>Today\'s priority</div><h2>All prepared applications are done.</h2><p class="spotlight-reason">Collect new postings or move on to interview prep.</p></div></div>';
    return;
  }
  container.innerHTML = `<div class="spotlight-grid"><div><div class="spotlight-kicker"><span class="live-dot"></span>Apply to this first · fit ${esc(role.score)}</div><div class="spotlight-company">${esc(role.company)}</div><h2>${esc(role.title)}</h2><p class="spotlight-reason">${esc(role.match)}</p><div class="spotlight-meta"><span class="glass-chip">${esc(role.location)}</span><span class="glass-chip">${esc(role.work_model)}</span><span class="glass-chip">${esc(role.salary)}</span></div></div><div class="spotlight-actions">${queueControl(role, 'primary-button') || externalApply(role, 'primary-button')}<button class="secondary-button" type="button" data-open-role="${esc(role.id)}">View materials</button><a class="secondary-button" href="${esc(materialUrl(role))}">Resume PDF</a></div></div>`;
}
function renderMetrics() {
  const stage = status => roles.filter(role => currentStatus(role) === status).length;
  const items = [[stage('discovered'), labelFor('discovered'), ''],[stage('materials_ready'), labelFor('materials_ready'), ''],[stage('applied'), labelFor('applied'), 'green'],[stage('interview'), labelFor('interview'), 'purple'],[stage('offer'), labelFor('offer'), 'orange']];
  document.getElementById('metrics').innerHTML = items.map(([value,label,tone]) => `<article class="metric"><div class="metric-top"><div class="metric-value">${value}</div><div class="metric-icon ${tone}">${icons.detail}</div></div><div class="metric-label">${label}</div></article>`).join('');
}
function roleMatches(role) {
  const query = document.getElementById('search').value.trim().toLowerCase();
  const tier = document.getElementById('tierFilter').value;
  const status = document.getElementById('statusFilter').value;
  const haystack = [role.company,role.title,role.location,role.work_model,role.match,...role.keywords].join(' ').toLowerCase();
  return (!query || haystack.includes(query)) && (locationFilter !== 'local' || isLocal(role)) && (locationFilter !== 'remote_bonus' || role.tier === 'remote_bonus') && (locationFilter !== 'relocation' || role.tier === 'relocation') && (tier === 'all' || role.tier === tier) && (status === 'all' || currentStatus(role) === status);
}
function renderRoles() {
  const filtered = roles.filter(roleMatches).sort((a,b) => b.score - a.score || b.posted.localeCompare(a.posted));
  setText('visibleCount', filtered.length);
  document.getElementById('empty').style.display = filtered.length ? 'none' : 'block';
  document.getElementById('roleList').innerHTML = filtered.map(role => {
    const scoreClass = role.score >= 8 ? 'high' : role.score < 7 ? 'low' : '';
    const queue = queueControl(role);
    return `<article class="role-row"><div class="score-ring ${scoreClass}" aria-label="Fit ${esc(role.score)}">${esc(role.score)}</div><div class="role-copy"><div class="role-company">${esc(role.company)}</div><div class="role-title">${esc(role.title)}</div><div class="role-match">${esc(role.match)}</div></div><div class="meta-stack"><div class="meta-primary">${esc(role.location)}</div><div class="meta-secondary">${esc(role.work_model)} · ${esc(role.salary)}</div></div>${statusSelect(role)}<div class="row-actions"><a class="row-action material-link" href="${esc(materialUrl(role))}" aria-label="Open ${esc(role.company)} materials" title="Open materials">${icons.document}</a>${queue || externalApply(role, 'row-action')}<button class="row-action" type="button" data-open-role="${esc(role.id)}" aria-label="${esc(role.company)} posting details" title="Posting details">${icons.detail}</button></div></article>`;
  }).join('');
}
function bindDynamicControls() {
  document.querySelectorAll('[data-status-id]').forEach(select => select.addEventListener('change', event => setStatus(event.currentTarget.dataset.statusId, event.currentTarget.value)));
  document.querySelectorAll('[data-open-role]').forEach(button => button.addEventListener('click', () => openRole(button.dataset.openRole)));
  document.querySelectorAll('[data-queue-role]').forEach(button => button.addEventListener('click', () => queueApplication(button.dataset.queueRole)));
}
function openRole(id) {
  const role = roles.find(item => item.id === id);
  if (!role) return;
  document.getElementById('dialogCompany').textContent = role.company;
  document.getElementById('dialogTitle').textContent = role.title;
  document.getElementById('dialogContent').innerHTML = `<div class="sheet-body"><div class="sheet-score"><strong>${esc(role.score)}</strong><span>/ 10 fit</span></div><div class="detail-grid"><div class="detail-cell"><div class="detail-label">Location</div><div class="detail-value">${esc(role.location)}</div></div><div class="detail-cell"><div class="detail-label">Work model</div><div class="detail-value">${esc(role.work_model)}</div></div><div class="detail-cell"><div class="detail-label">Compensation</div><div class="detail-value">${esc(role.salary)}</div></div><div class="detail-cell"><div class="detail-label">Posted · Channel</div><div class="detail-value">${formatDate(role.posted)} · ${esc(role.channel)}</div></div></div><div class="detail-section"><h3>Why it fits</h3><p>${esc(role.match)}</p></div><div class="detail-section"><h3>Requirements</h3><p>${esc(role.requirements)}</p></div><div class="detail-section"><h3>Key skills</h3><div class="keyword-list">${role.keywords.map(keyword => `<span class="keyword">${esc(keyword)}</span>`).join('')}</div></div><div class="detail-section"><h3>Application status</h3>${statusSelect(role, 'status-select dialog-status')}</div></div><div class="sheet-actions"><a class="link-button" href="${esc(materialUrl(role))}">PDF</a>${queueControl(role, 'primary-button') || externalApply(role, 'primary-button')}</div>`;
  const dialog = document.getElementById('roleDialog');
  dialog.showModal();
  bindDynamicControls();
}
function renderAll() { renderIntro(); renderSpotlight(); renderMetrics(); renderRoles(); bindDynamicControls(); }
document.getElementById('search').addEventListener('input', renderAll);
['tierFilter','statusFilter'].forEach(id => document.getElementById(id).addEventListener('change', renderAll));
document.querySelectorAll('[data-location]').forEach(button => button.addEventListener('click', () => { locationFilter = button.dataset.location; document.querySelectorAll('[data-location]').forEach(item => { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); }); renderAll(); }));
document.getElementById('closeDialog').addEventListener('click', () => document.getElementById('roleDialog').close());
document.getElementById('roleDialog').addEventListener('click', event => { if (event.target === event.currentTarget) event.currentTarget.close(); });
document.getElementById('resetBtn').addEventListener('click', () => {
  if (!confirm('Reset the application status changes saved in this browser?')) return;
  saved = {};
  removeStoredStatuses();
  renderAll();
  showToast('Saved statuses were reset.');
});
document.getElementById('exportBtn').addEventListener('click', () => {
  const output = {
    generated_at:new Date().toISOString(),
    provenance: isConnected
      ? {mode:'fixture_service', canonical_real_application_record:false}
      : {mode:'local_scratch', canonical_real_application_record:false},
    statuses:Object.fromEntries(roles.map(role => [role.id,currentStatus(role)]))
  };
  const blob = new Blob([JSON.stringify(output,null,2)], {type:'application/json'});
  const anchor = document.createElement('a');
  anchor.href = URL.createObjectURL(blob);
  anchor.download = isConnected ? 'fixture-status-export.json' : 'scratch-status-export.json';
  anchor.click();
  URL.revokeObjectURL(anchor.href);
  showToast(isConnected ? 'Saved the fixture status file. Not a real application record.' : 'Saved the scratch status file. Not a real application record.');
});
renderAll();
resetOfflineSurface();
loadConnectedState();
</script>
</body>
</html>
'''


def render_dashboard(data: dict) -> str:
    """Render a dashboard with JSON that cannot terminate its data script."""
    identity = PROFILE["identity"]
    embedded = (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    replacements = {
        "__GENERATED_AT__": html.escape(data["generated_at"]),
        "__JOB_DATA__": embedded,
        "__BRAND_NAME__": html.escape(identity["name"]),
        "__BRAND_INITIALS__": html.escape(identity["initials"]),
        "__BRAND_CITY__": html.escape(PROFILE["search"]["target_city_label"]),
        "__STORAGE_KEY__": html.escape(PROFILE["dashboard"]["storage_key"]),
        "__RESUME_PDF_NAME__": json.dumps(resume_pdf_name(PROFILE)).replace("<", "\\u003c"),
        "__STATUS_META__": json.dumps([list(pair) for pair in STATUSES]).replace("<", "\\u003c"),
        "__TIER_META__": json.dumps([list(pair) for pair in TIERS]).replace("<", "\\u003c"),
        "__STATUS_FILTER_OPTIONS__": "".join(
            f'<option value="{html.escape(value)}">{html.escape(label)}</option>' for value, label in STATUSES
        ),
        "__TIER_FILTER_OPTIONS__": "".join(
            f'<option value="{html.escape(value)}">{html.escape(label)}</option>' for value, label in TIERS
        ),
    }
    return re.sub(
        r"__GENERATED_AT__|__JOB_DATA__|__BRAND_NAME__|__BRAND_INITIALS__|__BRAND_CITY__|__STORAGE_KEY__"
        r"|__RESUME_PDF_NAME__|__STATUS_META__|__TIER_META__|__STATUS_FILTER_OPTIONS__|__TIER_FILTER_OPTIONS__",
        lambda match: replacements[match.group()],
        TEMPLATE,
    )


def main() -> None:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    OUTPUT.write_text(render_dashboard(data), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
