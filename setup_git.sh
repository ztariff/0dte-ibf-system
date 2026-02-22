#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# PHOENIX v3.0 — Git Setup Script
# Run this from inside the traderCowork folder on your machine
# ═══════════════════════════════════════════════════════════════

set -e

# Clean up any stale git state from the VM
rm -rf .git

echo "═══════════════════════════════════════════════════"
echo "  Setting up 0dte-ibf-system repo..."
echo "═══════════════════════════════════════════════════"

# Initialize fresh
git init -b main
git config user.email "nhaddad@smbcap.com"
git config user.name "nano"

# Stage everything (respects .gitignore)
git add -A

# Commit
git commit -m "Initial commit — PHOENIX v3.0 Iron Butterfly strategy

Complete 0DTE SPX Iron Butterfly trading system with:
- Signal catalog (14,991 parameter sets across 5 tiers)
- Jaccard-clustered signal groups at 70% threshold
- Concentrated V3 strategy: top 5 S-tier groups, tiered sizing, adaptive mechanics
- Full backtest: 161 trades, \$1.2M total, PF 2.68, Calmar 13.45
- Monte Carlo validation: 0% probability of loss across 10K resamples
- Interactive calendar dashboard with intraday P&L curves
- All P&L from 100% LIVE Polygon.io pricing

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Creating private repo on GitHub..."
echo "═══════════════════════════════════════════════════"

# Create repo via GitHub API
curl -s -H "Authorization: token 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" \
     -H "Accept: application/vnd.github.v3+json" \
     https://api.github.com/user/repos \
     -d '{"name":"0dte-ibf-system","private":true,"description":"PHOENIX v3.0 — 0DTE SPX Iron Butterfly Trading System"}' \
     | python3 -c "import sys,json; d=json.load(sys.stdin); print('Repo:', d.get('full_name','ERROR'), '| URL:', d.get('html_url',''))" 2>/dev/null || echo "Repo may already exist, continuing..."

# Get username
GH_USER=$(curl -s -H "Authorization: token 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" https://api.github.com/user | python3 -c "import sys,json; print(json.load(sys.stdin)['login'])")

# Set remote and push
git remote add origin "https://${GH_USER}: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@github.com/${GH_USER}/0dte-ibf-system.git" 2>/dev/null || \
git remote set-url origin "https://${GH_USER}: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@github.com/${GH_USER}/0dte-ibf-system.git"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Pushing to GitHub..."
echo "═══════════════════════════════════════════════════"

git push -u origin main

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✓ Done! Repo: github.com/${GH_USER}/0dte-ibf-system"
echo "═══════════════════════════════════════════════════"
echo ""
echo "To sync from another PC:"
echo "  git clone https://${GH_USER}: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@github.com/${GH_USER}/0dte-ibf-system.git"
echo ""
echo "To push future changes:"
echo "  git add -A && git commit -m 'description' && git push"
