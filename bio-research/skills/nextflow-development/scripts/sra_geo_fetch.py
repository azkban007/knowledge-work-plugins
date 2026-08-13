#!/usr/bin/env python3
"""
GEO/SRA Data Fetcher
====================
Download raw sequencing data from NCBI GEO/SRA and prepare for nf-core pipelines.

Usage:
    python sra_geo_fetch.py info <GEO_ID>              # Get study information
    python sra_geo_fetch.py list <GEO_ID>              # List all samples/runs
    python sra_geo_fetch.py download <GEO_ID> -o DIR   # Download FASTQ files
    python sra_geo_fetch.py samplesheet <GEO_ID> ...   # Generate samplesheet

Examples:
    python sra_geo_fetch.py info GSE110004
    python sra_geo_fetch.py list GSE110004 --filter "RNA-Seq:PAIRED"
    python sra_geo_fetch.py download GSE110004 -o ./fastq --parallel 4
    python sra_geo_fetch.py samplesheet GSE110004 --fastq-dir ./fastq -o samplesheet.csv
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Add utils to path
sys.path.insert(0, str(Path(__file__).parent))
from utils.ncbi_utils import (
    check_network_access,
    fetch_geo_metadata,
    fetch_sra_study_accession,
    fetch_sra_run_info,
    fetch_sra_run_info_detailed,
    fetch_ena_fastq_urls,
    download_file,
    format_file_size,
    estimate_download_size,
    group_samples_by_type,
    format_sample_groups_table,
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)
logger = logging.getLogger(__name__)

# Load genome mapping
SCRIPT_DIR = Path(__file__).parent
GENOMES_FILE = SCRIPT_DIR / "config" / "genomes.yaml"


@dataclass
class StudyInfo:
    """Information about a GEO study."""
    geo_id: str
    title: str
    organism: str
    n_samples: int
    summary: str
    sra_study: Optional[str]
    suggested_genome: Optional[str]
    suggested_pipeline: Optional[str]


def load_genome_mapping() -> Dict:
    """Load organism to genome mapping from config."""
    if not GENOMES_FILE.exists():
        return {}

    try:
        import yaml
        with open(GENOMES_FILE) as f:
            config = yaml.safe_load(f)
        return config.get('organisms', {})
    except ImportError:
        # Fallback: parse YAML manually for simple cases
        mapping = {}
        try:
            with open(GENOMES_FILE) as f:
                content = f.read()
            # Simple regex parsing for organism blocks
            pattern = r'"([^"]+)":\s*\n\s*genome:\s*"([^"]+)"'
            for match in re.finditer(pattern, content):
                mapping[match.group(1)] = {'genome': match.group(2)}
        except Exception:
            pass
        return mapping


def suggest_genome(organism: str) -> Optional[str]:
    """Suggest a genome based on organism name."""
    genome_map = load_genome_mapping()

    # Direct match
    if organism in genome_map:
        return genome_map[organism].get('genome')

    # Case-insensitive search
    organism_lower = organism.lower()
    for org_name, info in genome_map.items():
        if org_name.lower() == organism_lower:
            return info.get('genome')
        # Check aliases
        aliases = info.get('aliases', [])
        if any(alias.lower() == organism_lower for alias in aliases):
            return info.get('genome')

    # Common fallbacks
    fallbacks = {
        'homo sapiens': 'GRCh38',
        'human': 'GRCh38',
        'mus musculus': 'GRCm39',
        'mouse': 'GRCm39',
        'saccharomyces cerevisiae': 'R64-1-1',
        'yeast': 'R64-1-1',
        'drosophila melanogaster': 'BDGP6',
        'caenorhabditis elegans': 'WBcel235',
        'danio rerio': 'GRCz11',
        'arabidopsis thaliana': 'TAIR10',
        'rattus norvegicus': 'Rnor_6.0',
    }

    return fallbacks.get(organism_lower)


def suggest_pipeline(library_strategy: str, library_source: str = '') -> str:
    """Suggest nf-core pipeline based on library strategy."""
    strategy = library_strategy.upper()

    pipeline_map = {
        'RNA-SEQ': 'rnaseq',
        'ATAC-SEQ': 'atacseq',
        'CHIP-SEQ': 'chipseq',
        'WGS': 'sarek',
        'WXS': 'sarek',
        'AMPLICON': 'ampliseq',
        'BISULFITE-SEQ': 'methylseq',
        'HI-C': 'hic',
    }

    return pipeline_map.get(strategy, 'rnaseq')


def cmd_info(args):
    """Display study information."""
    geo_id = args.geo_id.upper()

    print(f"\nFetching information for {geo_id}...")

    # Check network
    network_ok, network_msg = check_network_access()
    if not network_ok:
        print(f"\n⚠️  Network issues detected:\n{network_msg}")

    # Get GEO metadata
    metadata = fetch_geo_metadata(geo_id)
    if not metadata:
        print(f"\n❌ Could not fetch metadata for {geo_id}")
        return 1

    # Get SRA study accession
    sra_study = fetch_sra_study_accession(geo_id)

    # Get detailed run info
    print("Fetching SRA run information...")
    runs = fetch_sra_run_info_detailed(geo_id)
    if not runs:
        # Fallback to basic method
        runs = fetch_sra_run_info(geo_id)

    # Group samples by type
    groups = group_samples_by_type(runs) if runs else {}

    # Suggest genome and pipeline
    organism = metadata.get('organism', 'Unknown')
    genome = suggest_genome(organism)

    # Determine primary data type
    primary_strategy = 'RNA-SEQ'
    if groups:
        primary_group = max(groups.items(), key=lambda x: x[1]['count'])
        primary_strategy = primary_group[1]['strategy']
    pipeline = suggest_pipeline(primary_strategy)

    # Estimate download size
    est_size = estimate_download_size(runs)

    # Display info
    print("\n" + "━" * 70)
    print(f"{geo_id}: {metadata.get('title', 'N/A')}")
    print("━" * 70)
    print(f"Organism:     {organism}")
    print(f"Samples:      {metadata.get('n_samples', 'N/A')}")
    print(f"SRA Study:    {sra_study or 'Not found'}")
    print(f"Runs:         {len(runs)}")
    print(f"Est. Size:    ~{format_file_size(est_size)}")
    print(f"Genome:       {genome or 'Unknown (manual selection required)'}")
    print(f"Pipeline:     nf-core/{pipeline} (suggested)")

    # Show sample groups table
    if groups:
        print(format_sample_groups_table(groups))

    if metadata.get('summary'):
        summary = metadata['summary']
        if len(summary) > 300:
            summary = summary[:297] + "..."
        print(f"\nSummary:\n  {summary}")

    print("━" * 70)

    # Show download hints
    if len(groups) > 1:
        print("\n💡 To download a specific subset, use:")
        for key in sorted(groups.keys()):
            print(f"   --subset \"{key}\"")

    # Save study info JSON
    if args.output_json:
        info = {
            'geo_id': geo_id,
            'title': metadata.get('title'),
            'organism': organism,
            'n_samples': metadata.get('n_samples'),
            'sra_study': sra_study,
            'n_runs': len(runs),
            'groups': {k: {**v, 'runs': None, 'gsm_ids': list(v.get('gsm_ids', []))} for k, v in groups.items()},
            'suggested_genome': genome,
            'suggested_pipeline': pipeline,
            'summary': metadata.get('summary'),
        }
        output_path = Path(args.output_json)
        with open(output_path, 'w') as f:
            json.dump(info, f, indent=2)
        print(f"\n📄 Study info saved to: {output_path}")

    return 0


def cmd_groups(args):
    """Display sample groups in a study for interactive selection."""
    geo_id = args.geo_id.upper()

    print(f"\nFetching sample groups for {geo_id}...")

    # Get detailed run info
    runs = fetch_sra_run_info_detailed(geo_id)
    if not runs:
        runs = fetch_sra_run_info(geo_id)

    if not runs:
        print(f"\n❌ No runs found for {geo_id}")
        return 1

    # Group samples
    groups = group_samples_by_type(runs)

    print(format_sample_groups_table(groups))

    # Output for interactive selection
    print("\n📋 Available groups for --subset option:")
    for i, (key, info) in enumerate(sorted(groups.items(), key=lambda x: -x[1]['count']), 1):
        size_str = format_file_size(info['size_estimate'])
        print(f"  {i}. \"{key}\" - {info['count']} samples (~{size_str})")

    # Save to JSON if requested
    if args.output:
        output_path = Path(args.output)
        output_data = {
            'geo_id': geo_id,
            'groups': {}
        }
        for key, info in groups.items():
            output_data['groups'][key] = {
                'count': info['count'],
                'gsm_range': info['gsm_range'],
                'gsm_ids': info.get('gsm_ids', []),
                'size_estimate': info['size_estimate'],
                'strategy': info['strategy'],
                'layout': info['layout'],
                'srr_ids': [r['srr'] for r in info['runs']],
            }
        with open(output_path, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"\n📄 Groups saved to: {output_path}")

    return 0


def cmd_list(args):
    """List all samples and runs in a study."""
    geo_id = args.geo_id.upper()

    print(f"\nFetching run list for {geo_id}...")

    runs = fetch_sra_run_info(geo_id)
    if not runs:
        print(f"\n❌ No runs found for {geo_id}")
        return 1

    # Apply filter if specified
    if args.filter:
        filter_parts = args.filter.split(':')
        strategy_filter = filter_parts[0].upper() if filter_parts else None
        layout_filter = filter_parts[1].upper() if len(filter_parts) > 1 else None

        filtered = []
        for run in runs:
            if strategy_filter and run.get('library_strategy', '').upper() != strategy_filter:
                continue
            if layout_filter and run.get('layout', '').upper() != layout_filter:
                continue
            filtered.append(run)
        runs = filtered

    print(f"\n{'SRR':<15} {'GSM':<12} {'Layout':<8} {'Strategy':<12} {'Size':>10}")
    print("-" * 60)

    for run in runs:
        size = format_file_size(run.get('bases', 0) // 4)
        print(f"{run['srr']:<15} {run.get('gsm', 'N/A'):<12} {run.get('layout', 'N/A'):<8} "
              f"{run.get('library_strategy', 'N/A'):<12} {size:>10}")

    print(f"\nTotal: {len(runs)} runs")

    # Output as TSV if requested
    if args.output:
        output_path = Path(args.output)
        with open(output_path, 'w') as f:
            f.write("run_accession\tgsm\tlayout\tlibrary_strategy\tbases\n")
            for run in runs:
                f.write(f"{run['srr']}\t{run.get('gsm', '')}\t{run.get('layout', '')}\t"
                        f"{run.get('library_strategy', '')}\t{run.get('bases', 0)}\n")
        print(f"\n📄 Run list saved to: {output_path}")

    return 0


def download_fastq_file(url: str, output_path: Path, timeout: int = 600) -> Tuple[str, bool]:
    """Download a single FASTQ file."""
    filename = output_path.name
    if output_path.exists():
        return filename, True  # Already exists

    success = download_file(url, output_path, timeout=timeout, show_progress=False)
    return filename, success


def interactive_select_group(groups: Dict[str, Dict]) -> Optional[str]:
    """Interactively select a sample group."""
    if len(groups) <= 1:
        return None  # No selection needed

    print("\n" + "=" * 60)
    print("  SELECT SAMPLE GROUP TO DOWNLOAD")
    print("=" * 60)

    sorted_groups = sorted(groups.items(), key=lambda x: -x[1]['count'])

    for i, (key, info) in enumerate(sorted_groups, 1):
        size_str = format_file_size(info['size_estimate'])
        print(f"\n  [{i}] {info['strategy']} ({info['layout'].lower()})")
        print(f"      Samples: {info['count']}")
        print(f"      GSM: {info['gsm_range']}")
        print(f"      Size: ~{size_str}")

    print(f"\n  [0] Download ALL ({sum(g['count'] for g in groups.values())} samples)")
    print("-" * 60)

    try:
        choice = input("\nEnter selection (0-{}): ".format(len(sorted_groups))).strip()
        choice_num = int(choice)

        if choice_num == 0:
            return None  # Download all
        elif 1 <= choice_num <= len(sorted_groups):
            selected_key = sorted_groups[choice_num - 1][0]
            print(f"\n✓ Selected: {selected_key}")
            return selected_key
        else:
            print("Invalid selection, downloading all.")
            return None
    except (ValueError, EOFError, KeyboardInterrupt):
        print("\nInvalid input, downloading all.")
        return None


def _fetch_runs_and_studies(geo_id: str) -> Tuple[List[Dict], List[str]]:
    """Fetch runs and collect unique SRA studies."""
    print("Fetching SRA run information...")
    runs = fetch_sra_run_info_detailed(geo_id)
    if not runs:
        runs = fetch_sra_run_info(geo_id)

    if not runs:
        print(f"❌ No runs found for {geo_id}")
        return [], []

    # Collect all unique SRA studies from runs (SuperSeries may have multiple)
    sra_studies = set(r.get('sra_study', '') for r in runs if r.get('sra_study'))
    if not sra_studies:
        print(f"❌ Could not find any SRA studies for {geo_id}")
        return [], []

    return runs, sorted(sra_studies)


def _display_study_info(sra_studies: List[str]) -> None:
    """Display SRA study information."""
    if len(sra_studies) > 1:
        print(f"SuperSeries detected with {len(sra_studies)} SRA studies: {', '.join(sra_studies)}")
    else:
        print(f"SRA Study: {sra_studies[0]}")


def _handle_subset_selection(groups: Dict[str, Dict], args) -> Optional[str]:
    """Handle subset selection logic (interactive or from args)."""
    selected_subset = args.subset

    # Show sample groups if multiple types exist
    if len(groups) > 1:
        print(format_sample_groups_table(groups))

    # Interactive mode if multiple groups and no subset specified
    if args.interactive and len(groups) > 1 and not selected_subset:
        selected_subset = interactive_select_group(groups)

    return selected_subset


def _fetch_fastq_urls(sra_studies: List[str]) -> Dict[str, List[str]]:
    """Fetch FASTQ URLs from ENA for all SRA studies."""
    print("\nFetching FASTQ URLs from ENA...")
    fastq_urls = {}
    for sra_study in sra_studies:
        study_urls = fetch_ena_fastq_urls(sra_study)
        if study_urls:
            print(f"  {sra_study}: {len(study_urls)} runs")
            fastq_urls.update(study_urls)
    return fastq_urls


def _apply_subset_filter(fastq_urls: Dict[str, List[str]], runs: List[Dict], selected_subset: str) -> Dict[str, List[str]]:
    """Apply subset filter to FASTQ URLs."""
    filter_parts = selected_subset.split(':')
    strategy_filter = filter_parts[0].upper() if filter_parts else None
    layout_filter = filter_parts[1].upper() if len(filter_parts) > 1 else None

    filtered_srrs = set()
    for run in runs:
        if strategy_filter and run.get('library_strategy', '').upper() != strategy_filter:
            continue
        if layout_filter and run.get('layout', '').upper() != layout_filter:
            continue
        filtered_srrs.add(run['srr'])

    filtered_urls = {srr: urls for srr, urls in fastq_urls.items() if srr in filtered_srrs}
    print(f"\n📦 Filtered to {len(filtered_urls)} runs matching \"{selected_subset}\"")
    return filtered_urls


def _count_files_and_check_existing(fastq_urls: Dict[str, List[str]], output_dir: Path) -> Tuple[int, int, List[Tuple[str, Path]]]:
    """Count total files and check for existing files."""
    total_files = sum(len(urls) for urls in fastq_urls.values())
    print(f"\n📦 Found {len(fastq_urls)} runs, {total_files} FASTQ files to download")

    # Check for existing files
    existing = 0
    downloads_needed = []
    for srr, urls in fastq_urls.items():
        for url in urls:
            filename = url.split('/')[-1]
            filepath = output_dir / filename
            if filepath.exists():
                existing += 1
            else:
                downloads_needed.append((url, filepath))

    if existing:
        print(f"  ✓ {existing} files already exist, skipping")

    return total_files, existing, downloads_needed


def _download_files_parallel(downloads_needed: List[Tuple[str, Path]], parallel: int, timeout: int) -> Tuple[int, List[str]]:
    """Download files in parallel."""
    successful = 0
    failed = []

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {
            executor.submit(download_fastq_file, url, filepath, timeout): filepath
            for url, filepath in downloads_needed
        }

        for i, future in enumerate(as_completed(futures), 1):
            filepath = futures[future]
            filename, success = future.result()
            status = "✓" if success else "✗"
            print(f"  [{i}/{len(downloads_needed)}] {status} {filename}")
            if success:
                successful += 1
            else:
                failed.append(filename)

    return successful, failed


def _download_files_sequential(downloads_needed: List[Tuple[str, Path]], timeout: int) -> Tuple[int, List[str]]:
    """Download files sequentially."""
    successful = 0
    failed = []

    for i, (url, filepath) in enumerate(downloads_needed, 1):
        filename = filepath.name
        print(f"  [{i}/{len(downloads_needed)}] Downloading {filename}...")
        success = download_file(url, filepath, timeout=timeout)
        if success:
            successful += 1
            print(f"    ✓ Done")
        else:
            failed.append(filename)
            print(f"    ✗ Failed")

    return successful, failed


def _print_download_summary(successful: int, existing: int, failed: List[str]) -> int:
    """Print download summary and return exit code."""
    print(f"\n📊 Download summary:")
    print(f"  ✓ Successful: {successful + existing}")
    print(f"  ✗ Failed: {len(failed)}")

    if failed:
        print(f"\nFailed downloads:")
        for f in failed:
            print(f"  - {f}")
        return 1

    return 0


def _save_download_metadata(geo_id: str, sra_studies: List[str], fastq_urls: Dict[str, List[str]], 
                            total_files: int, output_dir: Path) -> None:
    """Save download metadata to JSON file."""
    metadata_path = output_dir / "download_metadata.json"
    metadata = {
        'geo_id': geo_id,
        'sra_studies': sra_studies,
        'n_runs': len(fastq_urls),
        'n_files': total_files,
        'output_dir': str(output_dir.absolute()),
    }
    with open(metadata_path, 'w') as f:
        json.dump(metadata, f, indent=2)


def cmd_download(args):
    """Download FASTQ files from ENA."""
    geo_id = args.geo_id.upper()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nPreparing download for {geo_id}...")

    # Fetch runs and SRA studies
    runs, sra_studies = _fetch_runs_and_studies(geo_id)
    if not runs:
        return 1

    _display_study_info(sra_studies)

    # Group samples
    groups = group_samples_by_type(runs)

    # Handle subset selection
    selected_subset = _handle_subset_selection(groups, args)

    # Fetch FASTQ URLs from ENA
    fastq_urls = _fetch_fastq_urls(sra_studies)

    if not fastq_urls:
        print("❌ No FASTQ URLs found in ENA")
        print("Tip: Try using SRA toolkit directly with prefetch + fasterq-dump")
        return 1

    # Apply filter if specified
    if selected_subset:
        fastq_urls = _apply_subset_filter(fastq_urls, runs, selected_subset)

    # Count files and check existing
    total_files, existing, downloads_needed = _count_files_and_check_existing(fastq_urls, output_dir)

    if not downloads_needed:
        print("\n✅ All files already downloaded!")
        return 0

    print(f"  ↓ {len(downloads_needed)} files to download")
    print()

    # Download files
    if args.parallel > 1:
        successful, failed = _download_files_parallel(downloads_needed, args.parallel, args.timeout)
    else:
        successful, failed = _download_files_sequential(downloads_needed, args.timeout)

    # Print summary and get exit code
    exit_code = _print_download_summary(successful, existing, failed)

    if exit_code == 0:
        print(f"\n✅ All files downloaded to: {output_dir}")
        _save_download_metadata(geo_id, sra_studies, fastq_urls, total_files, output_dir)

    return exit_code


def cmd_samplesheet(args):
    """Generate samplesheet for nf-core pipeline."""
    geo_id = args.geo_id.upper()
    fastq_dir = Path(args.fastq_dir)
    output_path = Path(args.output)

    print(f"\nGenerating samplesheet for {geo_id}...")

    # Get run info
    runs = fetch_sra_run_info(geo_id)
    if not runs:
        print(f"❌ No runs found for {geo_id}")
        return 1

    # Get GEO metadata for sample naming
    metadata = fetch_geo_metadata(geo_id)
    organism = metadata.get('organism', 'Unknown') if metadata else 'Unknown'
    genome = suggest_genome(organism)

    # Detect pipeline from data
    strategies = set(r.get('library_strategy', 'RNA-SEQ') for r in runs)
    primary_strategy = list(strategies)[0] if strategies else 'RNA-SEQ'
    pipeline = args.pipeline or suggest_pipeline(primary_strategy)

    # Map SRR to local FASTQ files
    samples = []
    for run in runs:
        srr = run['srr']
        layout = run.get('layout', 'PAIRED')

        # Find FASTQ files
        if layout == 'PAIRED':
            r1 = fastq_dir / f"{srr}_1.fastq