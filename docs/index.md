---
layout: home
title: Home
nav_order: 1
description: Understand your Azure estate, investigate problems, and plan your next move with Azure Support Agent.
permalink: /
home_showcase: true
---

<div class="home-page" markdown="1">
<div class="home-hero" markdown="1">

# Azure Support Agent

<p class="home-headline">See your Azure estate.<br><span>Know where to act.</span></p>
<p class="home-intro">Connect architecture, operational signals, and investigation evidence in one place. Understand what needs attention—and review your next step before making a change.</p>
<div class="home-actions">
	<a class="home-action home-action-primary" href="{{ site.baseurl }}/getting-started/">Get started <span aria-hidden="true">&nbsp;→</span></a>
	<a class="home-action" href="{{ site.baseurl }}/reference/visual-tour/">Explore the visual tour</a>
</div>
<div class="home-context" role="group" aria-label="Product context">
	<span>Deployed in your subscription</span><span>Open source</span><span>Read-oriented starting point</span>
</div>
</div>

<div class="home-showcase" data-home-showcase markdown="1">
<div class="showcase-header">
	<h2 id="see-the-workflow">From estate context to a reviewed next step</h2>
	<span class="showcase-badge">Synthetic demonstration</span>
</div>
<nav class="showcase-navigation" data-showcase-navigation aria-label="Explore four application workflows">
	<a class="showcase-tab" id="showcase-tab-map" href="#showcase-map" data-showcase-tab><span class="showcase-step" aria-hidden="true">01</span><span>Map your estate</span></a>
	<a class="showcase-tab" id="showcase-tab-investigate" href="#showcase-investigate" data-showcase-tab><span class="showcase-step" aria-hidden="true">02</span><span>Investigate an incident</span></a>
	<a class="showcase-tab" id="showcase-tab-recovery" href="#showcase-recovery" data-showcase-tab><span class="showcase-step" aria-hidden="true">03</span><span>Review recovery gaps</span></a>
	<a class="showcase-tab" id="showcase-tab-coordinate" href="#showcase-coordinate" data-showcase-tab><span class="showcase-step" aria-hidden="true">04</span><span>Coordinate operations</span></a>
</nav>

<section class="showcase-panel" id="showcase-map" aria-labelledby="showcase-map-title" markdown="1">
<h3 id="showcase-map-title">Make the connections visible.</h3>
<p>Explore a connected application, inspect its resources, and put dependencies in context before assessing impact.</p>

{% include screenshot.html file="estate-architecture-canvas.png" title="A connected application in the Architecture canvas" caption="Inspect the synthetic checkout topology and resource palette. Modeled connections and illustrative costs are not verified traffic, deployed resources, or price quotes." eager=true %}

<a class="showcase-workflow" href="{{ site.baseurl }}/how-to/design-assessment/architectures-know-me/">Follow the architecture workflow <span aria-hidden="true">→</span></a>
</section>

<section class="showcase-panel" id="showcase-investigate" aria-labelledby="showcase-investigate-title" markdown="1">
<h3 id="showcase-investigate-title">Follow the evidence, not just the alert.</h3>
<p>Read competing hypotheses, supporting observations, uncertainty, and proposed next steps together.</p>

{% include screenshot.html file="core-investigation-result.png" title="Deep Investigation with hypotheses and supporting evidence" caption="This illustrative result shows how to review an investigation. It is not an executed AI session or proof of root cause; proposed actions were not applied." %}

<a class="showcase-workflow" href="{{ site.baseurl }}/how-to/core-workloads/dashboard-chat/">Follow the investigation workflow <span aria-hidden="true">→</span></a>
</section>

<section class="showcase-panel" id="showcase-recovery" aria-labelledby="showcase-recovery-title" markdown="1">
<h3 id="showcase-recovery-title">See the gaps behind a readiness score.</h3>
<p>Compare failure scenarios, resource-level outcomes, and missing evidence instead of treating unknown as healthy.</p>

{% include screenshot.html file="ops-recovery-scenario-matrix.png" title="Recovery scenarios with known gaps and unknown outcomes" caption="This seeded demonstration separates scenario outcomes and evidence limits. It is not a live recovery assessment, successful restore, or executed failover drill." %}

<a class="showcase-workflow" href="{{ site.baseurl }}/how-to/coverage/recovery-readiness/">Follow the recovery review <span aria-hidden="true">→</span></a>
</section>

<section class="showcase-panel" id="showcase-coordinate" aria-labelledby="showcase-coordinate-title" markdown="1">
<h3 id="showcase-coordinate-title">Bring workload signals into one view.</h3>
<p>Use Mission Control to review saved support-system results and choose which finding needs a closer look.</p>

{% include screenshot.html file="core-mission-board.png" title="Mission Control support-system results for a synthetic workload" caption="The board illustrates saved workload results across operational checks. Statuses and scores are synthetic examples, not current Azure health, complete collection, or certification." %}

<a class="showcase-workflow" href="{{ site.baseurl }}/how-to/core-workloads/mission-control/">Follow the Mission Control workflow <span aria-hidden="true">→</span></a>
</section>

<p class="showcase-note">Real application views, fictional example data. Each preview illustrates a separate workflow—not a single end-to-end execution. Open any image full-size to inspect the details.</p>
</div>

## Explore by outcome

<p class="home-section-intro">Start with the question you need to answer. Keep scope, source freshness, and evidence limits in view.</p>
<div class="home-outcomes">
	<article class="outcome-card" style="--card-accent: #0759ac;">
		<h3 id="outcome-investigations">Investigations</h3>
		<p>Connect symptoms, changes, and evidence. Preserve the reasoning behind your next step.</p>
		<div class="outcome-links">
			<a href="{{ site.baseurl }}/user-guide/core/chat-deep-investigation/">Chat &amp; Deep Investigation →</a>
			<a href="{{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/">Evidence Locker</a>
			<a href="{{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/">Case Files</a>
		</div>
	</article>
	<article class="outcome-card" style="--card-accent: #087c87;">
		<h3 id="outcome-architecture">Architecture</h3>
		<p>Discover application boundaries, explore dependencies, and assess design concerns.</p>
		<div class="outcome-links">
			<a href="{{ site.baseurl }}/user-guide/design-ownership/architectures/">Architectures &amp; resource context →</a>
			<a href="{{ site.baseurl }}/user-guide/workloads/">Workload discovery</a>
			<a href="{{ site.baseurl }}/user-guide/design-ownership/know-me/">Know-Me support knowledge</a>
		</div>
	</article>
	<article class="outcome-card" style="--card-accent: #7152a1;">
		<h3 id="outcome-governance">Governance</h3>
		<p>Review policy, identity, and access paths. Separate findings from coverage gaps.</p>
		<div class="outcome-links">
			<a href="{{ site.baseurl }}/user-guide/governance-identity/azure-policy/">Azure Policy →</a>
			<a href="{{ site.baseurl }}/user-guide/governance-identity/identity/">Entra ID</a>
			<a href="{{ site.baseurl }}/user-guide/governance-identity/iam/">IAM &amp; access paths</a>
		</div>
	</article>
	<article class="outcome-card" style="--card-accent: #aa6323;">
		<h3 id="outcome-recovery">Recovery</h3>
		<p>Identify protection gaps, inspect backup operations, and question recovery assumptions.</p>
		<div class="outcome-links">
			<a href="{{ site.baseurl }}/user-guide/coverage/recovery-readiness/">Recovery Readiness →</a>
			<a href="{{ site.baseurl }}/user-guide/coverage/backup-dr-coverage/">Backup &amp; DR Coverage</a>
			<a href="{{ site.baseurl }}/user-guide/coverage/backup-manager/">Backup Manager</a>
		</div>
	</article>
	<article class="outcome-card" style="--card-accent: #287d5e;">
		<h3 id="outcome-estate-intelligence">Estate intelligence</h3>
		<p>Understand inventory, cost, ownership, and change across your selected estate.</p>
		<div class="outcome-links">
			<a href="{{ site.baseurl }}/user-guide/estate-intelligence/inventory/">Inventory &amp; cost views →</a>
			<a href="{{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/">Change Explorer</a>
			<a href="{{ site.baseurl }}/user-guide/design-ownership/ownership/">Ownership &amp; accountability</a>
		</div>
	</article>
	<article class="outcome-card" style="--card-accent: #4766a0;">
		<h3 id="outcome-automation">Automation</h3>
		<p>Turn reviewed procedures into reusable operations, schedules, and notification routes.</p>
		<div class="outcome-links">
			<a href="{{ site.baseurl }}/user-guide/automations/">Tasks, Workbooks &amp; Playbooks →</a>
			<a href="{{ site.baseurl }}/user-guide/design-ownership/ai-insight-packs/">AI Insight Packs</a>
			<a href="{{ site.baseurl }}/connectors/">Connectors &amp; integrations</a>
		</div>
	</article>
</div>

## Start here

<div class="home-start">
	<article class="start-card">
		<h3 id="start-setup">Set up your instance</h3>
		<p>Review prerequisites, deploy into your subscription, and configure your first connection.</p>
		<div class="outcome-links"><a href="{{ site.baseurl }}/getting-started/one-click-install/">Installation guide →</a><a href="{{ site.baseurl }}/getting-started/first-run/">First-run configuration</a></div>
	</article>
	<article class="start-card">
		<h3 id="start-task">Complete a task</h3>
		<p>Follow a workflow with prerequisites, expected results, safety notes, and verification.</p>
		<div class="outcome-links"><a href="{{ site.baseurl }}/how-to/">Find a how-to guide →</a><a href="{{ site.baseurl }}/user-guide/">Explore every feature</a></div>
	</article>
	<article class="start-card">
		<h3 id="start-operate">Operate and extend</h3>
		<p>Configure access, providers, and integrations, or inspect how the application is built.</p>
		<div class="outcome-links"><a href="{{ site.baseurl }}/admin/">Administration →</a><a href="{{ site.baseurl }}/technical/">Technical documentation</a><a href="https://github.com/zmustafa/AzureSupportAgent">View on GitHub</a></div>
	</article>
</div>

<section class="home-safety" aria-labelledby="home-safety-title">
	<h2 id="home-safety-title">Understand first. Review before acting.</h2>
	<p>Start from a read-oriented posture and grant only the access each workflow requires. Before making changes, review the feature's permissions, confirmation and approval behavior, scope, and rollback options. A preview is not permission to execute. Read the <a href="{{ site.baseurl }}/security/">security overview</a>.</p>
</section>

## Need help?

<div class="home-help">
	<a href="{{ site.baseurl }}/reference/troubleshooting/">Troubleshooting</a>
	<a href="{{ site.baseurl }}/reference/permissions/">Permissions</a>
	<a href="{{ site.baseurl }}/reference/glossary/">Concepts &amp; glossary</a>
	<a href="{{ site.baseurl }}/reference/visual-tour/">Browse the screenshot collection</a>
</div>
</div>
