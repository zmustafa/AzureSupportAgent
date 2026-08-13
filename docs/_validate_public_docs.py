"""Validate public Jekyll page metadata, headings, hierarchy, and deterministic recipe sections."""
from __future__ import annotations
import re, sys
from collections import defaultdict
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parent
EX_DIR={"_site","_journey_render","deck-assets","improvement-plans","test-findings","usecase-render","_sass",".jekyll-cache"}
EX_FILE={"ARCHITECTURES_FEATURE.md","ARCHITECTURES_TEST_PLAN.md","BUG_HUNTING_PLAN.md","DATA_RETENTION_PLAN.md","GRAPH_TEST_PLAN.md","INVENTORY_TEST_PLAN.md","UI_TEST_PLAN.md","UX_ADVANCED_PLAN.md"}
FM=re.compile(r"^---\s*\n(.*?)\n---\s*\n",re.S)
errors=[]; pages=[]; titles=defaultdict(list); permalinks=defaultdict(list)
for p in sorted(ROOT.rglob("*.md")):
 rel=p.relative_to(ROOT)
 if rel.name in EX_FILE or any(x in EX_DIR for x in rel.parts): continue
 text=p.read_text(encoding="utf-8",errors="replace"); m=FM.match(text)
 if not m: continue
 try: meta=yaml.safe_load(m.group(1)) or {}
 except Exception as e: errors.append(f"{rel}: malformed YAML: {e}"); continue
 body=text[m.end():]; lines=body.splitlines(); headings=[]; in_fence=False
 for i,x in enumerate(lines):
  if x.lstrip().startswith('```'):
   in_fence=not in_fence
   continue
  if not in_fence and re.match(r'^#{1,6}\s',x): headings.append((i+1,len(x)-len(x.lstrip('#')),x))
 h1=[h for h in headings if h[1]==1]
 if len(h1)!=1: errors.append(f"{rel}: expected exactly one H1, found {len(h1)}")
 if h1 and any(h[1]>=2 and h[0]<h1[0][0] for h in headings): errors.append(f"{rel}: H1 appears after an H2+")
 normalized=[re.sub(r'\s+',' ',h[2].lstrip('#').strip().lower()) for h in headings]
 for name in ("troubleshooting","safety and limitations","safety and rollback"):
  if normalized.count(name)>1: errors.append(f"{rel}: duplicate '{name}' section")
 low=body.lower()
 if "issue observed while using this feature" in low or "re-run with correct scope" in low: errors.append(f"{rel}: placeholder troubleshooting text")
 title=str(meta.get('title','')).strip(); permalink=str(meta.get('permalink','')).strip()
 if title: titles[title].append(rel.as_posix())
 if permalink: permalinks[permalink].append(rel.as_posix())
 pages.append((rel,meta,normalized))
for title,owners in titles.items():
 if len(owners)>1: errors.append(f"duplicate title '{title}': {owners}")
for link,owners in permalinks.items():
 if len(owners)>1: errors.append(f"duplicate permalink '{link}': {owners}")
all_titles=set(titles)
for rel,meta,heads in pages:
 for key in ('parent','grand_parent'):
  value=meta.get(key)
  if value and str(value) not in all_titles: errors.append(f"{rel}: {key} '{value}' has no page title")
 if rel.parts and rel.parts[0]=='how-to' and rel.name!='index.md':
  for required in ('prerequisites','route','safety and rollback','troubleshooting','related docs'):
   if required not in heads: errors.append(f"{rel}: missing required section '{required}'")
  if not any(h.startswith('how to ') for h in heads): errors.append(f"{rel}: no verb-led 'How to ...' recipe")

# ---- Source-to-documentation coverage -------------------------------------------------
# /technical/documentation-regeneration/ promises that every current route, navigation item,
# permission area and connector maps to documentation. Nothing enforced that, so pages were
# free to claim feature ids that no longer existed (or never had). The inventory below is
# extracted from application source by _feature_inventory.py; these checks compare the two.
INV=ROOT/"_feature_inventory.json"
# Layer rules. The site is built in two layers and every visible area needs both: a feature
# reference that explains it and a how-to recipe that performs it.
REFERENCE_ROOTS={"user-guide","admin","connectors","security","reference","getting-started","technical"}
HOWTO_ROOTS={"how-to"}
# Namespaces that are catalogues rather than screens. They are explained in one reference
# table; demanding a numbered recipe per permission key would produce 89 useless pages.
REFERENCE_ONLY_NAMESPACES={"PERMISSION"}
if not INV.exists():
 errors.append("_feature_inventory.json is missing - run: python _feature_inventory.py")
else:
 import json
 inventory=json.loads(INV.read_text(encoding="utf-8"))
 known=set(inventory["feature_ids"])
 claimed_reference=defaultdict(list); claimed_howto=defaultdict(list)
 for rel,meta,_heads in pages:
  raw=meta.get("feature_ids") or []
  if isinstance(raw,str): raw=[raw]
  root=rel.parts[0] if len(rel.parts)>1 else ""
  for fid in raw:
   fid=str(fid).strip()
   if not fid: continue
   if fid not in known:
    errors.append(f"{rel}: feature_ids claims '{fid}', which does not exist in application source")
    continue
   if root in HOWTO_ROOTS: claimed_howto[fid].append(rel.as_posix())
   elif root in REFERENCE_ROOTS: claimed_reference[fid].append(rel.as_posix())
   else: errors.append(f"{rel}: feature_ids used outside a documentation layer")
 undocumented=[f for f in sorted(known) if f not in claimed_reference and f not in claimed_howto]
 for fid in undocumented:
  errors.append(f"undocumented feature: '{fid}' is in the application but on no page")
 missing_reference=[f for f in sorted(known)
   if f.split(":",1)[0] not in REFERENCE_ONLY_NAMESPACES
   and f not in claimed_reference and f in claimed_howto]
 for fid in missing_reference:
  errors.append(f"no feature reference for '{fid}' (how-to only) - add it under a reference-layer page")
 missing_howto=[f for f in sorted(known)
   if f.split(":",1)[0] not in REFERENCE_ONLY_NAMESPACES
   and f in claimed_reference and f not in claimed_howto]
 for fid in missing_howto:
  errors.append(f"no how-to recipe for '{fid}' (reference only) - add a numbered procedure under docs/how-to/")
 covered=len(known)-len(undocumented)
 print(f"feature ids: {covered}/{len(known)} documented (release {inventory['app_release']})")

print(f"public pages checked: {len(pages)}")
print(f"errors: {len(errors)}")
for e in errors: print(f"  {e}")
sys.exit(1 if errors else 0)
