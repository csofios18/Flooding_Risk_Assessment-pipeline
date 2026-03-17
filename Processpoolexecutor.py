#!/usr/bin/env python
# coding: utf-8

import os
import re
import subprocess
import time
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from pyswmm import Simulation, Nodes
import swmmio
import traceback

#  Modify INP File
def modify_inp_file(inp_file_path, pump_settings_file, output_inp_file_path):
    df = pd.read_csv(pump_settings_file, index_col="Name")
    pumps_off = df[df["Status"] == 0].index.tolist()

    with open(inp_file_path, "r") as file:
        lines = file.readlines()

    in_pumps_section = False
    modified_lines = []

    for line in lines:
        if line.strip().upper() == "[PUMPS]":
            in_pumps_section = True
            modified_lines.append(line)
            continue
        elif line.strip().startswith("[") and in_pumps_section:
            in_pumps_section = False

        if in_pumps_section and not line.strip().startswith(";") and line.strip():
            parts = re.split(r"\s+", line.strip())
            if len(parts) == 8 and parts[0] == "NESPS_Pump4_2_OMID" and parts[3] == "NESPS_Pump4" and parts[4] == "_2_OMID":
                parts[3] = "NESPS_Pump4\x1f_2_OMID"
                del parts[4]
            if len(parts) >= 7 and parts[0] in pumps_off:
                parts[5] = "1000"
                line = " ".join(parts) + "\n"
        modified_lines.append(line)

    report_lines = [
        "[REPORT]\n",
        "INPUT      YES\n",
        "CONTROLS   YES\n",
        "NODES      ALL\n",
        "LINKS      ALL\n"
    ]

    in_report_section = False
    report_found = False
    new_lines = []

    for line in modified_lines:
        if line.strip().upper() == "[REPORT]":
            in_report_section = True
            report_found = True
            new_lines.extend(report_lines)
            continue
        if in_report_section and line.strip().startswith("["):
            in_report_section = False
        if not in_report_section:
            new_lines.append(line)

    if not report_found:
        new_lines.append("\n")
        new_lines.extend(report_lines)

    with open(output_inp_file_path, "w") as file:
        file.writelines(new_lines)
    print(f"✅ Modified INP saved: {output_inp_file_path.name}")

# ▶ Run SWMM
def run_swmm_subprocess(swmm_exe, inp_file, rpt_file, out_file, tag, env):
    try:
        proc = subprocess.run(
            [swmm_exe, str(inp_file), str(rpt_file), str(out_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        print(f"✔ SWMM completed: {inp_file.name}")
        return (tag, True)
    except Exception as e:
        return (tag, f"❌ Failed: {inp_file.name} → {e}")

#  Extract Max Depths
def extract_max_depths_from_rpt(inp_file, rpt_file, output_csv_file):
    model = swmmio.Model(str(inp_file))
    model.rpt_path = str(rpt_file)
    if model.rpt is None or not hasattr(model.rpt, 'node_depth_summary') or model.rpt.node_depth_summary is None:
        print(f"⚠️ No depth data in {rpt_file.name}")
        return False
    df = model.rpt.node_depth_summary
    df = df[["MaxNodeDepth"]].copy()
    df.index.name = "Node"
    df.reset_index(inplace=True)
    df.rename(columns={"MaxNodeDepth": "Max_depth(SWMM)"}, inplace=True)
    df.to_csv(output_csv_file, index=False)
    print(f"📤 Saved depths: {output_csv_file.name}")
    return True

#  PySWMM Postprocessing
def extract_node_full_depths(inp_file):
    depths = []
    with Simulation(str(inp_file)) as sim:
        sim.step_advance(7200)
        for node in Nodes(sim):
            depths.append({"Node": node.nodeid, "Full_Depth": node.full_depth})
    return pd.DataFrame(depths)

def export_basement_flooding(inp_file, max_depth_csv, output_csv):
    full_depth_df = extract_node_full_depths(inp_file)
    max_depth_df = pd.read_csv(max_depth_csv)
    max_depth_df.rename(columns={max_depth_df.columns[0]: "Node"}, inplace=True)
    df = pd.merge(full_depth_df, max_depth_df, on="Node", how="left").fillna(0)
    df["basement flooding"] = df.apply(lambda r: 1 if r["Max_depth(SWMM)"] >= r["Full_Depth"] - 8 else 0, axis=1)
    df["Flooded_Depth"] = df.apply(lambda r: r["Max_depth(SWMM)"] - (r["Full_Depth"] - 8) if r["basement flooding"] else 0, axis=1)
    df.to_csv(output_csv, index=False)
    print(f"📂 Flooding results: {output_csv.name}")

#  Plot Flooding Map
def read_coordinates(inp_file):
    coords = {}
    with open(inp_file, 'r') as file:
        lines = file.readlines()
    in_coords = False
    for line in lines:
        if line.strip().upper() == '[COORDINATES]':
            in_coords = True
            continue
        if in_coords and line.strip().startswith('['):
            break
        if in_coords and line.strip() and not line.strip().startswith(';'):
            parts = line.split()
            if len(parts) >= 3:
                coords[parts[0]] = (float(parts[1]), float(parts[2]))
    return coords

def plot_flooding_map(inp_file, flood_csv, tag):
    coords = read_coordinates(inp_file)
    df = pd.read_csv(flood_csv)
    df['Node'] = df['Node'].astype(str)
    flooded = df[(df['basement flooding'] == 1) & (df['Node'].isin(coords))]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[coords[n][0] for n in coords],
        y=[coords[n][1] for n in coords],
        mode='markers', marker=dict(color='lightgrey', size=4), name='All Nodes', hoverinfo='skip'))
    fig.add_trace(go.Scatter(
        x=[coords[n][0] for n in flooded['Node']],
        y=[coords[n][1] for n in flooded['Node']],
        mode='markers', marker=dict(color='red', size=4),
        name='Flooded Nodes', text=flooded['Node'], hoverinfo='text'))
    fig.update_layout(title=f"Flooding Map: {tag}", height=700, width=900)
    fig.write_html(str(inp_file.parent/ f"{tag}_(35-45)_flooding_map.html"))
    print(f"🗜️ Map saved: {tag}_(35-45)_flooding_map.html")

#  MAIN PIPELINE
def simulate_and_postprocess(task):
    swmm_exe, inp_file, rpt_file, out_file, tag, env = task
    tag, status = run_swmm_subprocess(swmm_exe, inp_file, rpt_file, out_file, tag, env)
    if status is not True:
        print(status)
        return None

    try:
        max_csv = inp_file.parent/ f"{tag}(35-45)_depths.csv"
        flood_csv = inp_file.parent/ f"{tag}(35-45)_flooding.csv"
        extract_max_depths_from_rpt(inp_file, rpt_file, max_csv)
        export_basement_flooding(inp_file, max_csv, flood_csv)
        plot_flooding_map(inp_file, flood_csv, tag)
        return tag
    except Exception as e:
        print(f"❌ Post-processing failed for {tag}: {e}")
        return None

def run_pipeline():
    base_path = Path(r"C:\Users\csofios\Desktop\LongTermCSO\SWMM")
    inp_file_path = base_path / "GLWA_Core_v4_R05_2020_p2_harris_no_event_2.inp"
    pump_folder = base_path / "pump status(35-45mph)_withbackup"
    swmm_exe = r"C:\Program Files\EPA SWMM 5.2.4 (64-bit)\runswmm.exe"

    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "2"  # ✅ Set threads per SWMM run

    pump_files = sorted(pump_folder.glob("*.csv"))[1:81]# for first 80 replications
    # Only include pump status files 64, 65, and 67
    #target_tags = {"pump_status_64", "pump_status_65", "pump_status_67"}
    #pump_files = [f for f in pump_folder.glob("*.csv") if f.stem in target_tags]

    tasks = []

    for pump_file in pump_files:
        tag = pump_file.stem
        mod_inp = base_path/ f"{tag}_(35-45)_modified.inp"
        rpt_file = base_path/ f"{tag}_(35-45)_modified.rpt"
        out_file = base_path/ f"{tag}_(35-45)_modified.out"
        modify_inp_file(inp_file_path, pump_file, mod_inp)
        tasks.append((swmm_exe, mod_inp, rpt_file, out_file, tag, env))

    results = []
    with ProcessPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(simulate_and_postprocess, task) for task in tasks]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                print(f"❌ Simulation error: {e}")

    print(f"\n✅ Completed {len(results)} simulations")

if __name__ == "__main__":
    start = time.time()
    run_pipeline()
    print(f"\n✅ Total runtime: {(time.time() - start) / 60:.2f} minutes")
