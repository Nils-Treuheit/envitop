use crossterm::{
    cursor,
    event::{self, Event, KeyCode, KeyModifiers},
    execute, terminal,
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Cell, Paragraph, Row, Table, Wrap},
    Terminal,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    env, fs, io,
    path::Path,
    process::Command,
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

#[link(name = "sqlite3")]
extern "C" {
    fn sqlite3_open_v2(
        filename: *const libc::c_char,
        db: *mut *mut libc::c_void,
        flags: i32,
        vfs: *const libc::c_char,
    ) -> i32;
    fn sqlite3_close(db: *mut libc::c_void) -> i32;
    fn sqlite3_prepare_v2(
        db: *mut libc::c_void,
        sql: *const libc::c_char,
        n: i32,
        stmt: *mut *mut libc::c_void,
        tail: *mut *const libc::c_char,
    ) -> i32;
    fn sqlite3_step(stmt: *mut libc::c_void) -> i32;
    fn sqlite3_finalize(stmt: *mut libc::c_void) -> i32;
    fn sqlite3_column_text(stmt: *mut libc::c_void, i: i32) -> *const u8;
    fn sqlite3_column_int64(stmt: *mut libc::c_void, i: i32) -> i64;
}

fn read(path: &str) -> String {
    fs::read_to_string(path).unwrap_or_default()
}
fn num(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(Value::as_f64).unwrap_or(0.0)
}
fn ibytes(v: u64) -> String {
    let mut x = v as f64;
    let u = ["B", "K", "M", "G", "T", "P"];
    let mut i = 0;
    while x >= 1024.0 && i < u.len() - 1 {
        x /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{}{}", x as u64, u[i])
    } else if x >= 100.0 {
        format!("{x:.0}{}", u[i])
    } else {
        format!("{x:.1}{}", u[i])
    }
}
fn rate(v: f64) -> String {
    format!("{}/s", ibytes(v.max(0.0) as u64))
}
fn parse_cpu(line: &str) -> Vec<u64> {
    line.split_whitespace()
        .skip(1)
        .filter_map(|s| s.parse().ok())
        .collect()
}
fn cpu_percent(a: &[u64], b: &[u64]) -> (f64, f64) {
    let total: u64 = b
        .iter()
        .take(8)
        .sum::<u64>()
        .saturating_sub(a.iter().take(8).sum());
    let idle = b
        .get(3)
        .copied()
        .unwrap_or(0)
        .saturating_add(b.get(4).copied().unwrap_or(0))
        .saturating_sub(
            a.get(3)
                .copied()
                .unwrap_or(0)
                .saturating_add(a.get(4).copied().unwrap_or(0)),
        );
    let user = b
        .get(0)
        .copied()
        .unwrap_or(0)
        .saturating_add(b.get(1).copied().unwrap_or(0))
        .saturating_sub(
            a.get(0)
                .copied()
                .unwrap_or(0)
                .saturating_add(a.get(1).copied().unwrap_or(0)),
        );
    let d = total.max(1) as f64;
    (
        100.0 * (total.saturating_sub(idle)) as f64 / d,
        100.0 * user as f64 / d,
    )
}
fn cpu_stats() -> (Vec<u64>, Vec<Vec<u64>>) {
    let mut all = Vec::new();
    let mut cores = Vec::new();
    for line in read("/proc/stat").lines() {
        let tag = line.split_whitespace().next().unwrap_or("");
        if tag == "cpu" {
            all = parse_cpu(line)
        } else if tag
            .strip_prefix("cpu")
            .and_then(|x| x.parse::<usize>().ok())
            .is_some()
        {
            cores.push(parse_cpu(line));
        } else if !cores.is_empty() {
            break;
        }
    }
    (all, cores)
}
fn meminfo() -> HashMap<String, u64> {
    read("/proc/meminfo")
        .lines()
        .filter_map(|l| {
            let (k, v) = l.split_once(':')?;
            Some((
                k.to_owned(),
                v.split_whitespace()
                    .next()?
                    .parse::<u64>()
                    .ok()?
                    .saturating_mul(1024),
            ))
        })
        .collect()
}
fn netstats() -> (u64, u64, Value) {
    let (mut rx, mut tx) = (0, 0);
    let mut each = serde_json::Map::new();
    for l in read("/proc/net/dev").lines().skip(2) {
        if let Some((name, rest)) = l.split_once(':') {
            let n = name.trim();
            let a: Vec<u64> = rest
                .split_whitespace()
                .filter_map(|x| x.parse().ok())
                .collect();
            if a.len() >= 9 {
                rx += a[0];
                tx += a[8];
                each.insert(n.into(), json!({"rx":a[0],"tx":a[8]}));
            }
        }
    }
    (rx, tx, Value::Object(each))
}
fn interface_metrics(before: &Value, after: &Value, elapsed: f64) -> Vec<Value> {
    let old = before.as_object();
    after.as_object().into_iter().flat_map(|x|x.iter()).map(|(name,row)|{let rx=row["rx"].as_u64().unwrap_or(0);let tx=row["tx"].as_u64().unwrap_or(0);let previous=old.and_then(|x|x.get(name));let rx0=previous.and_then(|x|x["rx"].as_u64()).unwrap_or(rx);let tx0=previous.and_then(|x|x["tx"].as_u64()).unwrap_or(tx);json!({"name":name,"rx":rx,"tx":tx,"rx_rate":rx.saturating_sub(rx0)as f64/elapsed.max(0.001),"tx_rate":tx.saturating_sub(tx0)as f64/elapsed.max(0.001)})}).collect()
}
fn diskstats() -> (u64, u64, HashMap<String, (u64, u64)>, Value) {
    let (mut r, mut w) = (0, 0);
    let mut dev = HashMap::new();
    let mut trace = serde_json::Map::new();
    for l in read("/proc/diskstats").lines() {
        let v: Vec<&str> = l.split_whitespace().collect();
        if v.len() < 14 {
            continue;
        }
        let name = v[2];
        let rb = v[5].parse::<u64>().unwrap_or(0) * 512;
        let wb = v[9].parse::<u64>().unwrap_or(0) * 512;
        dev.insert(name.to_string(), (rb, wb));
        let base = Path::new("/sys/block").join(name).exists();
        if base {
            r += rb;
            w += wb;
        }
        trace.insert(
            name.into(),
            json!({"read_bytes":rb,"write_bytes":wb,"storage_device":base}),
        );
    }
    (r, w, dev, Value::Object(trace))
}
fn temps() -> (Option<f64>, Vec<Option<f64>>, Value) {
    let mut package: Option<f64> = None;
    let mut cores: Vec<Option<f64>> = Vec::new();
    let mut ccds: HashMap<usize, f64> = HashMap::new();
    let mut sensors = serde_json::Map::new();
    if let Ok(hs) = fs::read_dir("/sys/class/hwmon") {
        for h in hs.flatten() {
            let hp = h.path();
            let name = read(&hp.join("name").to_string_lossy()).trim().to_string();
            let mut rows = Vec::new();
            if let Ok(fs0) = fs::read_dir(&hp) {
                for f in fs0.flatten() {
                    let n = f.file_name().to_string_lossy().to_string();
                    if !n.starts_with("temp") || !n.ends_with("_input") {
                        continue;
                    }
                    let raw = read(&f.path().to_string_lossy()).trim().parse::<f64>().ok();
                    let Some(t) = raw.map(|x| x / 1000.0).filter(|x| *x > 0.0 && *x < 150.0) else {
                        continue;
                    };
                    let stem = n.trim_end_matches("_input");
                    let label = read(&hp.join(format!("{stem}_label")).to_string_lossy())
                        .trim()
                        .to_owned();
                    let l = label.to_lowercase();
                    if l.contains("package") || l.contains("tctl") || l == "cpu" {
                        package = Some(package.unwrap_or(0.0).max(t));
                    }
                    if let Some(i) = l
                        .strip_prefix("core ")
                        .and_then(|x| x.parse::<usize>().ok())
                    {
                        if cores.len() <= i {
                            cores.resize(i + 1, None)
                        }
                        cores[i] = Some(t)
                    }
                    if let Some(i) = l
                        .strip_prefix("tccd ")
                        .and_then(|x| x.parse::<usize>().ok())
                    {
                        ccds.insert(i, t);
                    }
                    rows.push(json!({"label":label,"current":t}));
                }
            }
            if !rows.is_empty() {
                sensors.insert(name, json!(rows));
            }
        }
    }
    if cores.is_empty() && !ccds.is_empty() {
        let mut ordered: Vec<(usize, f64)> = ccds.into_iter().collect();
        ordered.sort_by_key(|x| x.0);
        let count = fs::read_dir("/sys/devices/system/cpu")
            .ok()
            .map(|d| {
                d.flatten()
                    .filter(|e| {
                        e.file_name()
                            .to_string_lossy()
                            .strip_prefix("cpu")
                            .and_then(|x| x.parse::<usize>().ok())
                            .is_some()
                    })
                    .count()
            })
            .unwrap_or(ordered.len())
            .max(1);
        cores = (0..count)
            .map(|i| Some(ordered[(i * ordered.len() / count).min(ordered.len() - 1)].1))
            .collect();
    }
    (package, cores, Value::Object(sensors))
}
fn unescape_mount(value: &str) -> String {
    let chars: Vec<char> = value.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\\'
            && i + 3 < chars.len()
            && chars[i + 1..i + 4].iter().all(|x| ('0'..='7').contains(x))
        {
            let n = ((chars[i + 1] as u32 - '0' as u32) * 64)
                + ((chars[i + 2] as u32 - '0' as u32) * 8)
                + (chars[i + 3] as u32 - '0' as u32);
            if let Some(c) = char::from_u32(n) {
                out.push(c);
                i += 4;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}
fn mount_sources() -> Vec<Value> {
    let skip = [
        "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2", "devpts",
        "ramfs", "iso9660", "autofs", "efivarfs", "fusectl",
    ];
    let prefixes = ["/snap", "/var/lib/docker", "/var/lib/snapd"];
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for line in read("/proc/self/mountinfo").lines() {
        let Some((left, right)) = line.split_once(" - ") else {
            continue;
        };
        let before: Vec<&str> = left.split_whitespace().collect();
        let after: Vec<&str> = right.split_whitespace().collect();
        if before.len() < 5 || after.len() < 2 {
            continue;
        }
        let major_minor = before[2];
        let mount = unescape_mount(before[4]);
        let fstype = after[0];
        if skip.contains(&fstype) || prefixes.iter().any(|p| mount.starts_with(p)) {
            continue;
        }
        let sys = Path::new("/sys/dev/block").join(major_minor);
        let Ok(resolved_path) = fs::canonicalize(&sys) else {
            continue;
        };
        let Some(device) = resolved_path
            .file_name()
            .map(|x| x.to_string_lossy().to_string())
        else {
            continue;
        };
        if device.is_empty() || !seen.insert(device.clone()) {
            continue;
        }
        let Ok(c) = std::ffi::CString::new(mount.as_bytes()) else {
            continue;
        };
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
            continue;
        }
        let unit = if st.f_frsize > 0 {
            st.f_frsize as u64
        } else {
            st.f_bsize as u64
        };
        let total = st.f_blocks as u64 * unit;
        let free = st.f_bavail as u64 * unit;
        let used = total.saturating_sub(st.f_bfree as u64 * unit);
        out.push(json!({"mount":mount,"major_minor":major_minor,"fstype":fstype,"device":device,"total":total,"free":free,"used":used}));
    }
    out.sort_by(|a, b| {
        b["total"]
            .as_u64()
            .cmp(&a["total"].as_u64())
            .then(a["mount"].as_str().cmp(&b["mount"].as_str()))
    });
    out.truncate(6);
    out
}
fn mount_info(
    devs: &HashMap<String, (u64, u64)>,
    previous: &HashMap<String, (u64, u64)>,
    elapsed: f64,
) -> Vec<Value> {
    mount_sources().into_iter().map(|m|{let device=m["device"].as_str().unwrap_or("");let (r,w)=devs.get(device).copied().unwrap_or((0,0));let (old_r,old_w)=previous.get(device).copied().unwrap_or((r,w));let used=m["used"].as_u64().unwrap_or(0);let total=m["total"].as_u64().unwrap_or(0);let free=m["free"].as_u64().unwrap_or(0);let percent=if used+free>0{used as f64*100.0/(used+free)as f64}else{0.0};json!({"mount":m["mount"],"used":used,"total":total,"percent":percent,"free":free,"device":device,"read_rate":r.saturating_sub(old_r)as f64/elapsed.max(0.001),"write_rate":w.saturating_sub(old_w)as f64/elapsed.max(0.001),"read_history":[],"write_history":[]})}).collect()
}

fn processes(
    old: &HashMap<i32, u64>,
    elapsed: f64,
    totalmem: u64,
) -> (Vec<Value>, HashMap<i32, u64>, usize, usize) {
    let mut rows = Vec::new();
    let mut next = HashMap::new();
    let mut task = 0;
    let mut threads = 0;
    let Ok(es) = fs::read_dir("/proc") else {
        return (rows, next, 0, 0);
    };
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1) as f64;
    let cpus = fs::read_dir("/sys/devices/system/cpu")
        .ok()
        .map(|d| {
            d.flatten()
                .filter(|e| {
                    e.file_name()
                        .to_string_lossy()
                        .strip_prefix("cpu")
                        .and_then(|v| v.parse::<usize>().ok())
                        .is_some()
                })
                .count()
        })
        .unwrap_or(1)
        .max(1) as f64;
    let boot = read("/proc/stat")
        .lines()
        .find_map(|l| l.strip_prefix("btime ").and_then(|v| v.parse::<f64>().ok()))
        .unwrap_or(0.0);
    for e in es.flatten() {
        let Ok(pid) = e.file_name().to_string_lossy().parse::<i32>() else {
            continue;
        };
        let st = read(&e.path().join("stat").to_string_lossy());
        let Some(end) = st.rfind(')') else { continue };
        let name = st[..end].trim_start_matches('(').to_owned();
        let f: Vec<&str> = st[end + 1..].split_whitespace().collect();
        if f.len() < 22 {
            continue;
        }
        task += 1;
        let th = f[17].parse::<usize>().unwrap_or(0);
        threads += th;
        let ticks = f[11].parse::<u64>().unwrap_or(0) + f[12].parse::<u64>().unwrap_or(0);
        next.insert(pid, ticks);
        let cpu = old
            .get(&pid)
            .map(|p| {
                (ticks.saturating_sub(*p) as f64 / hz / elapsed.max(0.001) * 100.0 / cpus)
                    .min(100.0)
            })
            .unwrap_or(0.0);
        let rss = f[21].parse::<u64>().unwrap_or(0) * 4096;
        let status = read(&e.path().join("status").to_string_lossy());
        let uid = status
            .lines()
            .find_map(|l| {
                l.strip_prefix("Uid:")
                    .and_then(|v| v.split_whitespace().next()?.parse::<u32>().ok())
            })
            .unwrap_or(0);
        let cmd = read(&e.path().join("cmdline").to_string_lossy())
            .replace('\0', " ")
            .trim()
            .to_owned();
        let command = cmd.trim().to_owned();
        let cmdline: Vec<String> = read(&e.path().join("cmdline").to_string_lossy())
            .split('\0')
            .filter(|x| !x.is_empty())
            .map(str::to_owned)
            .collect();
        let state = match f[0] {
            "R" => "running",
            "S" => "sleeping",
            "D" => "disk-sleep",
            "T" | "t" => "stopped",
            "Z" => "zombie",
            "I" => "idle",
            _ => "unknown",
        };
        let username = read("/etc/passwd")
            .lines()
            .find_map(|l| {
                let x: Vec<&str> = l.split(':').collect();
                (x.len() > 2 && x[2] == uid.to_string()).then(|| x[0].to_owned())
            })
            .unwrap_or_else(|| uid.to_string());
        rows.push(json!({"pid":pid,"name":name,"user":username,"cpu":cpu,"mem_pct":if totalmem>0{rss as f64*100.0/totalmem as f64}else{0.0},"rss":rss,"cmd":command.chars().take(400).collect::<String>(),"status":state,"threads":th,"ppid":f[1].parse::<i32>().unwrap_or(0),"cwd":fs::read_link(e.path().join("cwd")).ok().map(|p|p.to_string_lossy().to_string()).unwrap_or_default(),"create_time":boot+f[19].parse::<f64>().unwrap_or(0.0)/hz,"cmdline":cmdline,"gpu_index":null,"gpu_sm":null,"gpu_mem_pct":null,"gpu_vram":0}));
    }
    rows.sort_by(|a, b| num(b, "cpu").total_cmp(&num(a, "cpu")));
    (rows, next, task, threads)
}
fn gpu(processes: &[Value], uptime: f64) -> Value {
    let out = Command::new("nvidia-smi")
        .args(["--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem,pstate", "--format=csv,noheader,nounits"])
        .output();
    let Ok(out) = out else {
        return json!({"available":false,"error":"nvidia-smi not found","gpus":[],"processes":[],"driver":"","cuda":""});
    };
    if !out.status.success() {
        return json!({"available":false,"error":"NVIDIA GPU unavailable","gpus":[],"processes":[],"driver":"","cuda":""});
    }
    let header = Command::new("nvidia-smi")
        .output()
        .ok()
        .map(|x| String::from_utf8_lossy(&x.stdout).to_string())
        .unwrap_or_default();
    let mut driver = String::new();
    let mut cuda = String::new();
    for l in header.lines() {
        if let Some((_, v)) = l.split_once("Driver Version:") {
            driver = v.trim().split_whitespace().next().unwrap_or("").to_owned()
        }
        if let Some((_, v)) = l.split_once("CUDA Version:") {
            cuda = v.trim().split_whitespace().next().unwrap_or("").to_owned()
        }
    }
    let mut gpus = Vec::new();
    for (i, line) in String::from_utf8_lossy(&out.stdout).lines().enumerate() {
        let v: Vec<&str> = line.split(',').map(str::trim).collect();
        if v.len() < 12 {
            continue;
        }
        let n = |k: usize| v.get(k).and_then(|x| x.parse::<f64>().ok());
        let used = n(3).unwrap_or(0.0) as u64 * 1048576;
        let total = n(4).unwrap_or(0.0) as u64 * 1048576;
        gpus.push(json!({"index":v[0].parse::<usize>().unwrap_or(i),"name":v[1],"util":n(2).unwrap_or(0.0),"mem_used":used,"mem_total":total,"mem_percent":if total>0{100.0*used as f64/total as f64}else{0.0},"temp_c":n(5),"power_w":n(6),"power_limit_w":n(7),"fan_pct":n(8),"clock_sm":n(9),"clock_mem":n(10),"pstate":v[11],"encoder":null,"decoder":null,"history":[],"mem_history":[]}));
    }
    let mut apps: HashMap<i32, u64> = HashMap::new();
    if let Ok(x) = Command::new("nvidia-smi")
        .args([
            "--query-compute-apps=pid,used_memory",
            "--format=csv,noheader,nounits",
        ])
        .output()
    {
        if x.status.success() {
            for l in String::from_utf8_lossy(&x.stdout).lines() {
                let v: Vec<&str> = l.split(',').map(str::trim).collect();
                if v.len() >= 2 {
                    if let (Ok(pid), Ok(mib)) = (v[0].parse::<i32>(), v[1].parse::<u64>()) {
                        apps.insert(pid, mib * 1048576);
                    }
                }
            }
        }
    }
    let mut gpu_procs = Vec::new();
    if let Ok(x) = Command::new("nvidia-smi")
        .args(["pmon", "-s", "um", "-c", "1"])
        .output()
    {
        if x.status.success() {
            let mut cols: Vec<String> = Vec::new();
            for l in String::from_utf8_lossy(&x.stdout).lines() {
                let l = l.trim();
                if let Some(h) = l.strip_prefix('#') {
                    let fields: Vec<String> = h.split_whitespace().map(str::to_owned).collect();
                    if fields.iter().any(|v| v == "pid") && fields.iter().any(|v| v == "gpu") {
                        cols = fields;
                    }
                    continue;
                }
                if l.is_empty() {
                    continue;
                }
                let v: Vec<&str> = l.split_whitespace().collect();
                if v.len() < 3 {
                    continue;
                }
                let (Ok(gi), Ok(pid)) = (v[0].parse::<usize>(), v[1].parse::<i32>()) else {
                    continue;
                };
                if pid <= 0 {
                    continue;
                }
                let metric = |name: &str| {
                    cols.iter()
                        .position(|x| x == name)
                        .and_then(|i| v.get(i))
                        .and_then(|x| x.parse::<f64>().ok())
                };
                let p = processes
                    .iter()
                    .find(|x| x["pid"].as_i64() == Some(pid as i64));
                let procname = p
                    .and_then(|x| x["name"].as_str())
                    .map(str::to_owned)
                    .unwrap_or_else(|| {
                        let x = read(&format!("/proc/{pid}/comm")).trim().to_owned();
                        if x.is_empty() {
                            v.last().unwrap_or(&"?").to_string()
                        } else {
                            x
                        }
                    });
                let cmd = p
                    .and_then(|x| x["cmd"].as_str())
                    .unwrap_or(&procname)
                    .to_owned();
                let start = p.and_then(|x| x["create_time"].as_f64()).unwrap_or(0.0);
                let runtime = if start > 0.0 {
                    Some(
                        (SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .unwrap_or_default()
                            .as_secs_f64()
                            - start)
                            .max(0.0),
                    )
                } else {
                    None
                };
                let vram = metric("fb")
                    .filter(|x| *x >= 0.0)
                    .map(|x| (x * 1048576.0) as u64)
                    .or_else(|| apps.get(&pid).copied());
                gpu_procs.push(json!({"gpu_index":gi,"pid":pid,"name":procname,"kind":v.get(2).copied().unwrap_or("C"),"sm":metric("sm"),"gmbw_pct":metric("mem"),"vram":vram,"user":p.and_then(|x|x["user"].as_str()).unwrap_or(""),"runtime_s":runtime,"cpu_pct":p.and_then(|x|x["cpu"].as_f64()),"host_mem_pct":p.and_then(|x|x["mem_pct"].as_f64()),"rss":p.and_then(|x|x["rss"].as_u64()).unwrap_or(0),"cmd":cmd}));
            }
        }
    }
    if gpu_procs.is_empty() {
        for (pid, vram) in apps {
            let p = processes
                .iter()
                .find(|x| x["pid"].as_i64() == Some(pid as i64));
            let name = p.and_then(|x| x["name"].as_str()).unwrap_or("?");
            gpu_procs.push(json!({"gpu_index":gpus.first().and_then(|x|x["index"].as_u64()).unwrap_or(0),"pid":pid,"name":name,"kind":"C","sm":null,"gmbw_pct":null,"vram":vram,"user":p.and_then(|x|x["user"].as_str()).unwrap_or(""),"runtime_s":null,"cpu_pct":p.and_then(|x|x["cpu"].as_f64()),"host_mem_pct":p.and_then(|x|x["mem_pct"].as_f64()),"rss":p.and_then(|x|x["rss"].as_u64()).unwrap_or(0),"cmd":p.and_then(|x|x["cmd"].as_str()).unwrap_or(name)}));
        }
    }
    gpu_procs.sort_by(|a, b| {
        a["gpu_index"]
            .as_u64()
            .cmp(&b["gpu_index"].as_u64())
            .then_with(|| b["vram"].as_u64().cmp(&a["vram"].as_u64()))
            .then_with(|| a["pid"].as_i64().cmp(&b["pid"].as_i64()))
    });
    let _ = uptime;
    json!({"available":!gpus.is_empty(),"error":if gpus.is_empty(){"No NVIDIA GPUs reported"}else{""},"gpus":gpus,"processes":gpu_procs,"driver":driver,"cuda":cuda})
}
fn jsonl_files(
    root: &Path,
    out: &mut Vec<(std::path::PathBuf, SystemTime)>,
    depth: u8,
    cutoff: SystemTime,
) {
    if depth > 8 {
        return;
    }
    let Ok(rd) = fs::read_dir(root) else { return };
    for ent in rd.flatten() {
        let path = ent.path();
        let Ok(meta) = ent.metadata() else { continue };
        if meta.is_dir() {
            jsonl_files(&path, out, depth + 1, cutoff)
        } else if path.extension().and_then(|x| x.to_str()) == Some("jsonl") {
            if let Ok(mt) = meta.modified() {
                if mt >= cutoff {
                    out.push((path, mt));
                }
            }
        }
    }
}
fn agent_file(path: &Path, kind: &str) -> Option<Value> {
    let data = fs::read(path).ok()?;
    let tail = if data.len() > 1_500_000 {
        &data[data.len() - 1_500_000..]
    } else {
        &data
    };
    let text = String::from_utf8_lossy(tail);
    let mut id = path.file_stem()?.to_string_lossy().to_string();
    let (mut cwd, mut model, mut task) = (String::new(), String::new(), String::new());
    let (mut input, mut output, mut cache_read, mut cache_write, mut turns) =
        (0u64, 0u64, 0u64, 0u64, 0u64);
    let (mut started, mut updated) = (0.0, 0.0);
    let mut hist = Vec::new();
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if let Some(x) = v["sessionId"].as_str() {
            id = x.to_owned()
        }
        if let Some(x) = v["session_id"].as_str() {
            id = x.to_owned()
        }
        if let Some(x) = v["cwd"].as_str() {
            cwd = x.to_owned()
        }
        if let Some(x) = v["model"].as_str() {
            model = x.to_owned()
        }
        let ts = v["timestamp"]
            .as_str()
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.0);
        if ts > 0.0 {
            if started == 0.0 {
                started = ts
            }
            updated = ts
        }
        if kind == "claude" {
            let msg = &v["message"];
            let usage = &msg["usage"];
            if !usage.is_null() {
                input += usage["input_tokens"].as_u64().unwrap_or(0);
                output += usage["output_tokens"].as_u64().unwrap_or(0);
                cache_read += usage["cache_read_input_tokens"].as_u64().unwrap_or(0);
                cache_write += usage["cache_creation_input_tokens"].as_u64().unwrap_or(0);
                turns += 1;
                hist.push(output + input + cache_write)
            }
            if v["type"] == "user" {
                let content = v["message"]["content"].as_str().unwrap_or("");
                if !content.is_empty() {
                    task = content
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ")
                        .chars()
                        .take(180)
                        .collect()
                }
            }
        } else {
            let p = &v["payload"];
            if v["type"] == "session_meta" {
                if let Some(x) = p["id"].as_str() {
                    id = x.to_owned()
                }
                if let Some(x) = p["cwd"].as_str() {
                    cwd = x.to_owned()
                }
            }
            if p["type"] == "token_count" {
                let u = &p["info"]["total_token_usage"];
                input = u["input_tokens"].as_u64().unwrap_or(input);
                output = u["output_tokens"].as_u64().unwrap_or(output);
                cache_read = u["cached_input_tokens"].as_u64().unwrap_or(cache_read);
                let cu = &p["info"]["last_token_usage"];
                cache_write = cu["cache_creation_input_tokens"]
                    .as_u64()
                    .unwrap_or(cache_write);
                hist.push(input + output + cache_read + cache_write)
            }
            if v["type"] == "turn_context" {
                model = p["model"].as_str().unwrap_or(&model).to_owned()
            }
            if v["type"] == "response_item" && p["type"] == "message" && p["role"] == "user" {
                if let Some(content) = p["content"].as_array() {
                    for c in content {
                        if let Some(x) = c["text"].as_str() {
                            task = x
                                .split_whitespace()
                                .collect::<Vec<_>>()
                                .join(" ")
                                .chars()
                                .take(180)
                                .collect();
                        }
                    }
                }
            }
        }
    }
    let mt = fs::metadata(path)
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(updated);
    if updated == 0.0 {
        updated = mt
    }
    if started == 0.0 {
        started = updated
    }
    if cwd.is_empty() {
        cwd = path
            .parent()
            .and_then(|x| x.file_name())
            .map(|x| x.to_string_lossy().to_string())
            .unwrap_or_default()
    }
    let project = cwd.rsplit('/').next().unwrap_or(&cwd).to_string();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let status = if now - updated < 90.0 {
        "thinking"
    } else {
        "idle"
    };
    let all = input + output + cache_read + cache_write;
    let ctx = input + output + cache_read;
    Some(
        json!({"agent":kind,"session_id":id,"project":project,"cwd":cwd,"pid":0,"status":status,"model":model,"effort":"","context_pct":null,"context_tokens":ctx,"context_window":0,"input_tokens":input,"output_tokens":output,"cache_read":cache_read,"cache_write":cache_write,"total_tokens":all,"turn_count":turns,"started_at":started,"updated_at":updated,"mem_mb":0,"git_branch":"","git_added":0,"git_modified":0,"version":"","current_task":task,"summary":"","children":[],"token_history":hist,"chat":[],"tools":[],"source_path":path.to_string_lossy(),"subagent_count":0}),
    )
}
fn collect_opencode(scope: &str) -> Vec<Value> {
    let home = env::var("HOME").unwrap_or_default();
    let path = env::var("OPENCODE_DB")
        .unwrap_or_else(|_| format!("{home}/.local/share/opencode/opencode.db"));
    if !Path::new(&path).exists() {
        return Vec::new();
    }
    let file = match std::ffi::CString::new(path.clone()) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let mut db: *mut libc::c_void = std::ptr::null_mut();
    if unsafe { sqlite3_open_v2(file.as_ptr(), &mut db, 1, std::ptr::null()) } != 0 {
        return Vec::new();
    }
    let age = if scope == "all" { 86400 } else { 1800 };
    let cutoff = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
        - age * 1000;
    let query=std::ffi::CString::new(format!("SELECT id,title,directory,parent_id,model,tokens_input,tokens_output,tokens_reasoning,tokens_cache_read,tokens_cache_write,time_created,time_updated,version FROM session WHERE time_updated >= {cutoff} ORDER BY time_updated DESC LIMIT 30")).unwrap();
    let mut stmt: *mut libc::c_void = std::ptr::null_mut();
    let mut out = Vec::new();
    if unsafe { sqlite3_prepare_v2(db, query.as_ptr(), -1, &mut stmt, std::ptr::null_mut()) } == 0 {
        while unsafe { sqlite3_step(stmt) } == 100 {
            let strcol = |i| unsafe {
                let p = sqlite3_column_text(stmt, i);
                if p.is_null() {
                    String::new()
                } else {
                    std::ffi::CStr::from_ptr(p as *const libc::c_char)
                        .to_string_lossy()
                        .into_owned()
                }
            };
            let intcol = |i| unsafe { sqlite3_column_int64(stmt, i).max(0) as u64 };
            let parent = strcol(3);
            if !parent.is_empty() {
                continue;
            }
            let id = strcol(0);
            let title = strcol(1);
            let cwd = strcol(2);
            let model = strcol(4);
            let input = intcol(5);
            let output = intcol(6);
            let reasoning = intcol(7);
            let cr = intcol(8);
            let cw = intcol(9);
            let started = intcol(10) as f64 / 1000.0;
            let updated = intcol(11) as f64 / 1000.0;
            let version = strcol(12);
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            let age = now - updated;
            let status = if age < 15.0 {
                "thinking"
            } else if age < 180.0 {
                "waiting"
            } else {
                "idle"
            };
            let project = cwd
                .trim_end_matches('/')
                .rsplit('/')
                .next()
                .unwrap_or(&title);
            let total = input + output + reasoning + cr + cw;
            out.push(json!({"agent":"opencode","session_id":id,"project":project,"cwd":cwd,"pid":0,"status":status,"model":model,"effort":"","context_pct":null,"context_tokens":input+output+cr,"context_window":0,"input_tokens":input,"output_tokens":output,"cache_read":cr,"cache_write":cw,"total_tokens":total,"turn_count":0,"started_at":started,"updated_at":updated,"mem_mb":0,"git_branch":"","git_added":0,"git_modified":0,"version":version,"current_task":title,"summary":title,"children":[],"token_history":[],"chat":[],"tools":[],"source_path":path,"subagent_count":0}));
        }
    }
    if !stmt.is_null() {
        unsafe {
            sqlite3_finalize(stmt);
        }
    }
    unsafe {
        sqlite3_close(db);
    };
    out
}

fn collect_agents(scope: &str) -> Vec<Value> {
    let now = SystemTime::now();
    let age = if scope == "all" {
        Duration::from_secs(86400)
    } else {
        Duration::from_secs(1800)
    };
    let cutoff = now.checked_sub(age).unwrap_or(UNIX_EPOCH);
    let home = env::var("HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| Path::new("/").to_path_buf());
    let mut results = Vec::new();
    let mut roots = vec![
        (home.join(".claude/projects"), "claude"),
        (home.join(".codex/sessions"), "codex"),
    ];
    if let Ok(x) = env::var("CLAUDE_CONFIG_DIR") {
        roots.push((Path::new(&x).join("projects"), "claude"))
    }
    for (root, kind) in roots {
        let mut files = Vec::new();
        jsonl_files(&root, &mut files, 0, cutoff);
        files.sort_by_key(|x| std::cmp::Reverse(x.1));
        files.truncate(if kind == "claude" { 40 } else { 50 });
        for (p, _) in files {
            if let Some(s) = agent_file(&p, kind) {
                results.push(s)
            }
        }
    }
    results.extend(collect_opencode(scope));
    results.sort_by(|a, b| {
        b["updated_at"]
            .as_f64()
            .partial_cmp(&a["updated_at"].as_f64())
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    results
}

fn collect_quotas() -> Vec<Value> {
    let home = env::var("HOME").unwrap_or_default();
    let mut out = Vec::new();
    let mut paths = vec![
        (format!("{home}/.claude/abtop-rate-limits.json"), "claude"),
        (
            format!("{home}/.cache/abtop/codex-rate-limits.json"),
            "codex",
        ),
    ];
    if let Ok(x) = env::var("CLAUDE_CONFIG_DIR") {
        paths.insert(0, (format!("{x}/abtop-rate-limits.json"), "claude"));
    }
    for (path, source) in paths {
        let Ok(data) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(v) = serde_json::from_str::<Value>(&data) else {
            continue;
        };
        let five = &v["five_hour"];
        let seven = &v["seven_day"];
        if five.is_null() && seven.is_null() {
            continue;
        }
        out.push(json!({"source":v["source"].as_str().unwrap_or(source),"five_pct":five["used_percentage"],"five_reset":five["resets_at"],"five_window":five["window_minutes"],"seven_pct":seven["used_percentage"],"seven_reset":seven["resets_at"],"seven_window":seven["window_minutes"],"updated_at":v["updated_at"],"live":false}));
    }
    out
}
#[derive(Default)]
struct ProfileTiming {
    system_start: u128,
    system_end: u128,
    gpu_start: u128,
    gpu_end: u128,
    agent_start: u128,
    agent_end: u128,
    prime_wait_ms: f64,
}

fn snapshot(scope: &str, mut timing: Option<&mut ProfileTiming>) -> Value {
    if let Some(t) = timing.as_deref_mut() {
        t.system_start = monotonic_ns();
    }
    let (c0, cores0) = cpu_stats();
    let net_t0 = monotonic_ns();
    let (rx0, tx0, net_before) = netstats();
    let net_t0 = net_t0.max(monotonic_ns());
    let disk_t0 = monotonic_ns();
    let (rd0, wr0, previous_devs, _) = diskstats();
    let disk_t0 = disk_t0.max(monotonic_ns());
    emit_source_counters("prime_after_counters");
    let (_, proc_old, _, _) = processes(&HashMap::new(), 1.0, 1);
    let wait_start = monotonic_ns();
    thread::sleep(Duration::from_millis(350));
    if let Some(t) = timing.as_deref_mut() {
        t.prime_wait_ms = (monotonic_ns().saturating_sub(wait_start)) as f64 / 1_000_000.0;
    }
    let elapsed = 0.35;
    let (c1, cores1) = cpu_stats();
    let (cpu, user_pct) = cpu_percent(&c0, &c1);
    let core_pct: Vec<f64> = cores1
        .iter()
        .enumerate()
        .map(|(i, c)| cpu_percent(cores0.get(i).map(Vec::as_slice).unwrap_or(&[]), c).0)
        .collect();
    let m = meminfo();
    let total = *m.get("MemTotal").unwrap_or(&0);
    let avail = *m.get("MemAvailable").unwrap_or(&0);
    let used = total.saturating_sub(avail);
    let cached = m.get("Cached").unwrap_or(&0) + m.get("SReclaimable").unwrap_or(&0);
    let buffers = *m.get("Buffers").unwrap_or(&0);
    let st = *m.get("SwapTotal").unwrap_or(&0);
    let su = st.saturating_sub(*m.get("SwapFree").unwrap_or(&0));
    let (rx, tx, nettrace) = netstats();
    let net_t1 = monotonic_ns();
    let net_elapsed = (net_t1.saturating_sub(net_t0)) as f64 / 1_000_000_000.0;
    let (rd, wr, devs, disktrace) = diskstats();
    let disk_t1 = monotonic_ns();
    let disk_elapsed = (disk_t1.saturating_sub(disk_t0)) as f64 / 1_000_000_000.0;
    emit_source_counters("measured_after_counters");
    let (temp, ctemps, sensors) = temps();
    let uptime = read("/proc/uptime")
        .split_whitespace()
        .next()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.0);
    let (mut procs, _next, tasks, threads) = processes(&proc_old, elapsed, total);
    let load: Vec<f64> = read("/proc/loadavg")
        .split_whitespace()
        .take(3)
        .filter_map(|x| x.parse().ok())
        .collect();
    let model = read("/proc/cpuinfo")
        .lines()
        .find_map(|l| {
            l.split_once(':')
                .filter(|(k, _)| {
                    k.trim().eq_ignore_ascii_case("model name")
                        || k.trim().eq_ignore_ascii_case("hardware")
                })
                .map(|(_, v)| v.trim().to_owned())
        })
        .unwrap_or_else(|| "CPU".into());
    let mhz = fs::read_to_string("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .map(|x| x / 1000.0)
        .unwrap_or(0.0);
    let interfaces = interface_metrics(&net_before, &nettrace, net_elapsed);
    let (nrx, ntx) = (
        (rx.saturating_sub(rx0)) as f64 / net_elapsed.max(0.001),
        (tx.saturating_sub(tx0)) as f64 / net_elapsed.max(0.001),
    );
    let (nrd, nwr) = (
        (rd.saturating_sub(rd0)) as f64 / disk_elapsed.max(0.001),
        (wr.saturating_sub(wr0)) as f64 / disk_elapsed.max(0.001),
    );
    let host = read("/etc/hostname").trim().to_owned();
    let username = env::var("USER").unwrap_or_default();
    let os = read("/etc/os-release")
        .lines()
        .find_map(|l| {
            l.strip_prefix("PRETTY_NAME=")
                .map(|s| s.trim_matches('"').to_owned())
        })
        .unwrap_or_else(|| "Linux".into());
    let kernel = read("/proc/version").trim().to_owned();
    let cpus = cores1.len();
    let ctemp_json: Vec<Value> = if ctemps.is_empty() {
        vec![Value::Null; cpus]
    } else {
        (0..cpus)
            .map(|i| {
                ctemps
                    .get(i)
                    .and_then(|x| *x)
                    .map(|x| json!(x))
                    .unwrap_or(Value::Null)
            })
            .collect()
    };
    let core_temp_source = if ctemps.iter().any(Option::is_some) {
        if sensors
            .as_object()
            .into_iter()
            .flat_map(|x| x.values())
            .any(|v| {
                v.as_array().into_iter().flatten().any(|r| {
                    r["label"]
                        .as_str()
                        .unwrap_or("")
                        .to_lowercase()
                        .starts_with("tccd")
                })
            })
        {
            "CCD"
        } else {
            "core"
        }
    } else {
        ""
    };
    let disk_temp = sensors["nvme"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|x| x["current"].as_f64())
        .filter(|x| *x > 0.0 && *x < 150.0)
        .max_by(f64::total_cmp);
    let nettot = nettrace
        .as_object()
        .map(|x| {
            x.values()
                .map(|v| v["rx"].as_u64().unwrap_or(0))
                .sum::<u64>()
        })
        .unwrap_or(rx);
    let nettout = nettrace
        .as_object()
        .map(|x| {
            x.values()
                .map(|v| v["tx"].as_u64().unwrap_or(0))
                .sum::<u64>()
        })
        .unwrap_or(tx);
    let mounts = mount_info(&devs, &previous_devs, disk_elapsed);
    if let Some(t) = timing.as_deref_mut() {
        t.system_end = monotonic_ns();
        t.gpu_start = monotonic_ns();
    }
    let gpu = gpu(&procs, uptime);
    if let Some(t) = timing.as_deref_mut() {
        t.gpu_end = monotonic_ns();
        t.agent_start = monotonic_ns();
    }
    if let Some(rows) = gpu["processes"].as_array() {
        for gp in rows {
            let pid = gp["pid"].as_i64();
            if let Some(proc) = procs.iter_mut().find(|p| p["pid"].as_i64() == pid) {
                proc["gpu_index"] = gp["gpu_index"].clone();
                proc["gpu_sm"] = gp["sm"].clone();
                proc["gpu_mem_pct"] = gp["gmbw_pct"].clone();
                proc["gpu_vram"] = gp["vram"].as_u64().map(|x| json!(x)).unwrap_or(json!(0));
            }
        }
    }
    let mut agents = collect_agents(scope);
    for agent in &mut agents {
        let kind = agent["agent"].as_str().unwrap_or("");
        let source = agent["source_path"].as_str().unwrap_or("");
        if kind == "opencode" {
            continue;
        }
        let mut matched = None;
        for proc in &procs {
            let pid = proc["pid"].as_i64().unwrap_or(0);
            if pid <= 1 {
                continue;
            }
            let class = proc["cmd"].as_str().unwrap_or("").to_lowercase();
            if !class.contains(kind) {
                continue;
            }
            if let Ok(entries) = fs::read_dir(format!("/proc/{pid}/fd")) {
                for fd in entries.flatten() {
                    if fs::read_link(fd.path())
                        .ok()
                        .map(|p| p.to_string_lossy() == source)
                        .unwrap_or(false)
                    {
                        matched = Some(pid);
                        break;
                    }
                }
            }
            if matched.is_some() {
                break;
            }
            if matched.is_none() {
                matched = Some(pid)
            }
        }
        if let Some(pid) = matched {
            agent["pid"] = json!(pid);
        }
    }
    let quotas = collect_quotas();
    if let Some(t) = timing.as_deref_mut() {
        t.agent_end = monotonic_ns();
    }
    let trace = json!({"cpu":{"cpu":c1,"cpu0":cores1.get(0).cloned().unwrap_or_default()},"mem":{"MemTotal":total/1024,"MemAvailable":avail/1024,"Cached":m.get("Cached").unwrap_or(&0)/1024,"SReclaimable":m.get("SReclaimable").unwrap_or(&0)/1024,"Buffers":buffers/1024,"SwapTotal":st/1024,"SwapFree":m.get("SwapFree").unwrap_or(&0)/1024},"net":nettrace,"disk":disktrace,"cpu_model":model,"loadavg":load,"cpu_freq_mhz":mhz,"sensors":sensors,"mounts":mounts,"proc":{}});
    let _ = (trace, nettot, nettout);
    let mut result = json!({"ts":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64(),"hostname":host,"username":username,"uptime":uptime,"os_name":os,"kernel":kernel,"cpu":{"model_name":model,"percent":cpu,"user_percent":user_pct,"per_core":core_pct,"core_temps":ctemp_json,"core_temp_source":core_temp_source,"freq_mhz":mhz,"temp_c":temp,"load1":load.get(0).copied().unwrap_or(0.0),"load5":load.get(1).copied().unwrap_or(0.0),"load15":load.get(2).copied().unwrap_or(0.0),"history":[],"user_history":[],"core_history":[]},"mem":{"total":total,"used":used,"available":avail,"cached":cached,"buffers":buffers,"percent":if total>0{used as f64*100.0/total as f64}else{0.0},"swap_total":st,"swap_used":su,"swap_percent":if st>0{su as f64*100.0/st as f64}else{0.0},"history":[],"swap_history":[]},"net":{"rx_rate":nrx,"tx_rate":ntx,"total_rx":nettot,"total_tx":nettout,"interfaces":interfaces,"rx_history":[],"tx_history":[]},"disk":{"read_rate":nrd,"write_rate":nwr,"read_history":[],"write_history":[],"mounts":mounts,"temp_c":disk_temp},"gpu":gpu,"procs":procs,"task_count":tasks,"thread_count":threads,"agents":agents,"quotas":quotas,"token_rate":0.0,"agent_scope":scope,"errors":[]});
    seed_history(&mut result);
    result
}
fn history(old: &Value, key: &str, value: Value) -> Value {
    let mut a = old[key].as_array().cloned().unwrap_or_default();
    a.push(value);
    if a.len() > 120 {
        a.drain(..a.len() - 120);
    }
    json!(a)
}
fn seed_history(s: &mut Value) {
    let cpu = s["cpu"].clone();
    s["cpu"]["history"] = json!([cpu["percent"]]);
    s["cpu"]["user_history"] = json!([cpu["user_percent"]]);
    let cores = cpu["per_core"].as_array().cloned().unwrap_or_default();
    s["cpu"]["core_history"] = json!(cores.iter().map(|x| vec![x.clone()]).collect::<Vec<_>>());
    let mem = s["mem"].clone();
    s["mem"]["history"] = json!([mem["percent"]]);
    s["mem"]["swap_history"] = json!([mem["swap_percent"]]);
    let net = s["net"].clone();
    s["net"]["rx_history"] = json!([net["rx_rate"]]);
    s["net"]["tx_history"] = json!([net["tx_rate"]]);
    let disk = s["disk"].clone();
    s["disk"]["read_history"] = json!([disk["read_rate"]]);
    s["disk"]["write_history"] = json!([disk["write_rate"]]);
    if let Some(ms) = s["disk"]["mounts"].as_array_mut() {
        for m in ms {
            m["read_history"] = json!([m["read_rate"]]);
            m["write_history"] = json!([m["write_rate"]]);
        }
    }
    if let Some(gs) = s["gpu"]["gpus"].as_array_mut() {
        for g in gs {
            g["history"] = json!([g["util"]]);
            g["mem_history"] = json!([g["mem_percent"]]);
        }
    }
}
fn merge_history(s: &mut Value, old: &Value) {
    let old_tokens: u64 = old["agents"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|a| a["total_tokens"].as_u64().unwrap_or(0))
        .sum();
    let new_tokens: u64 = s["agents"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|a| a["total_tokens"].as_u64().unwrap_or(0))
        .sum();
    let dt = s["ts"].as_f64().unwrap_or(0.0) - old["ts"].as_f64().unwrap_or(0.0);
    let token_rate = if dt > 0.0 && s["agent_scope"] == old["agent_scope"] {
        new_tokens.saturating_sub(old_tokens) as f64 / dt
    } else {
        0.0
    };
    s["token_rate"] = json!(token_rate);
    for (section, key, value) in [
        ("cpu", "history", s["cpu"]["percent"].clone()),
        ("cpu", "user_history", s["cpu"]["user_percent"].clone()),
        ("mem", "history", s["mem"]["percent"].clone()),
        ("mem", "swap_history", s["mem"]["swap_percent"].clone()),
        ("net", "rx_history", s["net"]["rx_rate"].clone()),
        ("net", "tx_history", s["net"]["tx_rate"].clone()),
        ("disk", "read_history", s["disk"]["read_rate"].clone()),
        ("disk", "write_history", s["disk"]["write_rate"].clone()),
    ] {
        s[section][key] = history(&old[section], key, value);
    }
    let nc = s["cpu"]["per_core"].as_array().cloned().unwrap_or_default();
    let oc = old["cpu"]["core_history"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    s["cpu"]["core_history"] = json!(nc
        .iter()
        .enumerate()
        .map(|(i, v)| {
            let mut h = oc
                .get(i)
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            h.push(v.clone());
            if h.len() > 120 {
                h.drain(..h.len() - 120);
            }
            h
        })
        .collect::<Vec<_>>());
    if let Some(ms) = s["disk"]["mounts"].as_array_mut() {
        for m in ms {
            let path = m["mount"].as_str().unwrap_or("").to_owned();
            let prev = old["disk"]["mounts"]
                .as_array()
                .and_then(|xs| {
                    xs.iter()
                        .find(|x| x["mount"].as_str() == Some(path.as_str()))
                })
                .cloned()
                .unwrap_or(Value::Null);
            m["read_history"] = history(&prev, "read_history", m["read_rate"].clone());
            m["write_history"] = history(&prev, "write_history", m["write_rate"].clone());
        }
    }
    if let Some(gs) = s["gpu"]["gpus"].as_array_mut() {
        for g in gs {
            let idx = g["index"].as_u64();
            let prev = old["gpu"]["gpus"]
                .as_array()
                .and_then(|xs| xs.iter().find(|x| x["index"].as_u64() == idx))
                .cloned()
                .unwrap_or(Value::Null);
            g["history"] = history(&prev, "history", g["util"].clone());
            g["mem_history"] = history(&prev, "mem_history", g["mem_percent"].clone());
        }
    }
}

fn json_mode(scope: &str, profile: bool) {
    let process_start = monotonic_ns();
    let system_start = monotonic_ns();
    let mut timing = ProfileTiming::default();
    let s = snapshot(scope, if profile { Some(&mut timing) } else { None });
    let system_end = timing.system_end;
    let serialize_start = monotonic_ns();
    let serialized = serde_json::to_string(&s).unwrap_or_else(|_| "{}".into());
    let serialize_end = monotonic_ns();
    println!("{serialized}");
    if profile {
        let complete = monotonic_ns();
        let ms = |a: u128, b: u128| (b.saturating_sub(a)) as f64 / 1_000_000.0;
        let record = json!({"profile_version":1,"clock":"monotonic_ns","process_start_ns":process_start,"system_start_ns":system_start,"system_end_ns":system_end,"gpu_start_ns":timing.gpu_start,"gpu_end_ns":timing.gpu_end,"agent_start_ns":timing.agent_start,"agent_end_ns":timing.agent_end,"serialize_start_ns":serialize_start,"serialize_end_ns":serialize_end,"complete_ns":complete,"prime_wait_ms":timing.prime_wait_ms,"system_ms":ms(system_start,system_end),"system_work_ms":(ms(system_start,system_end)-timing.prime_wait_ms).max(0.0),"gpu_ms":ms(timing.gpu_start,timing.gpu_end),"agent_ms":ms(timing.agent_start,timing.agent_end),"serialize_ms":ms(serialize_start,serialize_end),"wall_ms":ms(process_start,complete),"system_prime_calls":1,"system_sample_calls":1,"gpu_calls":1,"agent_calls":1,"json_encodes":1});
        eprintln!("{}", record);
    }
}
fn trace_fd_write(value: &Value) {
    let Ok(fd) = env::var("ENVITOP_TRACE_FD")
        .ok()
        .and_then(|v| v.parse::<i32>().ok())
        .ok_or(())
    else {
        return;
    };
    let mut line = serde_json::to_vec(value).unwrap_or_default();
    line.push(b'\n');
    unsafe {
        libc::write(fd, line.as_ptr() as *const libc::c_void, line.len());
    }
}
fn monotonic_ns() -> u128 {
    let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec.max(0) as u128 * 1_000_000_000 + ts.tv_nsec.max(0) as u128
}
fn emit_source_counters(stage: &str) {
    if env::var_os("ENVITOP_TRACE_FD").is_none() {
        return;
    }
    let stamp = monotonic_ns();
    let (cpu, cores) = cpu_stats();
    let mut cpuobj = serde_json::Map::new();
    cpuobj.insert("cpu".into(), json!(cpu));
    for (i, c) in cores.iter().enumerate() {
        cpuobj.insert(format!("cpu{i}"), json!(c));
    }
    let mi = meminfo();
    let mem = json!({"MemTotal":mi.get("MemTotal").copied().unwrap_or(0)/1024,"MemAvailable":mi.get("MemAvailable").copied().unwrap_or(0)/1024,"Cached":mi.get("Cached").copied().unwrap_or(0)/1024,"SReclaimable":mi.get("SReclaimable").copied().unwrap_or(0)/1024,"Buffers":mi.get("Buffers").copied().unwrap_or(0)/1024,"SwapTotal":mi.get("SwapTotal").copied().unwrap_or(0)/1024,"SwapFree":mi.get("SwapFree").copied().unwrap_or(0)/1024});
    let (_, _, net) = netstats();
    let (_, _, _, disk) = diskstats();
    let (temp, _, sensors) = temps();
    let _ = temp;
    let model = read("/proc/cpuinfo")
        .lines()
        .find_map(|l| {
            l.split_once(':')
                .filter(|(k, _)| {
                    k.trim().eq_ignore_ascii_case("model name")
                        || k.trim().eq_ignore_ascii_case("hardware")
                })
                .map(|(_, v)| v.trim().to_owned())
        })
        .unwrap_or_else(|| "CPU".into());
    let load: Vec<f64> = read("/proc/loadavg")
        .split_whitespace()
        .take(3)
        .filter_map(|x| x.parse().ok())
        .collect();
    let freq = fs::read_to_string("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        .ok()
        .and_then(|x| x.trim().parse::<f64>().ok())
        .map(|x| x / 1000.0)
        .unwrap_or(0.0);
    let mounts = mount_sources();
    let mut proc = serde_json::Map::new();
    if let Ok(es) = fs::read_dir("/proc") {
        for e in es.flatten() {
            let Ok(pid) = e.file_name().to_string_lossy().parse::<i32>() else {
                continue;
            };
            let stat = read(&e.path().join("stat").to_string_lossy());
            let Some(end) = stat.rfind(')') else { continue };
            let f: Vec<&str> = stat[end + 1..].split_whitespace().collect();
            if f.len() < 22 {
                continue;
            }
            let uid = read(&e.path().join("status").to_string_lossy())
                .lines()
                .find_map(|l| {
                    l.strip_prefix("Uid:")
                        .and_then(|x| x.split_whitespace().next()?.parse::<u64>().ok())
                })
                .unwrap_or(0);
            let name = stat[..end].trim_start_matches('(').to_owned();
            proc.insert(pid.to_string(),json!({"name":name,"uid":uid,"utime":f[11].parse::<u64>().unwrap_or(0),"stime":f[12].parse::<u64>().unwrap_or(0),"threads":f[17].parse::<u64>().unwrap_or(0),"starttime":f[19].parse::<u64>().unwrap_or(0),"rss":f[21].parse::<u64>().unwrap_or(0)*4096}));
        }
    }
    trace_fd_write(
        &json!({"event":"source_counters","stage":stage,"monotonic_ns":stamp,"sources":{"cpu":cpuobj,"mem":mem,"net":net,"disk":disk,"cpu_model":model,"loadavg":load,"cpu_freq_mhz":freq,"sensors":sensors,"mounts":mounts,"proc":proc}}),
    );
}
fn emit_tui_trace(s: &Value, seq: u64) {
    if env::var_os("ENVITOP_TRACE_FD").is_none() {
        return;
    }
    let cpu = &s["cpu"];
    let mem = &s["mem"];
    let net = &s["net"];
    let disk = &s["disk"];
    let gpu = &s["gpu"];
    let cores=cpu["per_core"].as_array().cloned().unwrap_or_default().iter().enumerate().map(|(i,v)|json!({"percent":v,"temp_c":cpu["core_temps"].get(i).cloned().unwrap_or(Value::Null)})).collect::<Vec<_>>();
    let processes=s["procs"].as_array().cloned().unwrap_or_default().iter().map(|p|json!({"cpu":p["cpu"],"mem_pct":p["mem_pct"],"rss":p["rss"],"threads":p["threads"]})).collect::<Vec<_>>();
    let gpus=gpu["gpus"].as_array().cloned().unwrap_or_default().iter().map(|g|json!({"util":g["util"],"mem_percent":g["mem_percent"],"mem_used":g["mem_used"],"mem_total":g["mem_total"],"temp_c":g["temp_c"]})).collect::<Vec<_>>();
    let gp=gpu["processes"].as_array().cloned().unwrap_or_default().iter().map(|p|json!({"sm":p["sm"],"gmbw_pct":p["gmbw_pct"],"vram":p["vram"],"runtime_s":p["runtime_s"]})).collect::<Vec<_>>();
    let mounts=disk["mounts"].as_array().cloned().unwrap_or_default().iter().map(|m|json!({"used":m["used"],"total":m["total"],"free":m["free"],"read_rate":m["read_rate"],"write_rate":m["write_rate"]})).collect::<Vec<_>>();
    let agents = s["agents"].as_array().cloned().unwrap_or_default();
    let total_tokens: u64 = agents
        .iter()
        .map(|a| a["total_tokens"].as_u64().unwrap_or(0))
        .sum();
    let values = json!({"cpu":{"percent":cpu["percent"],"user_percent":cpu["user_percent"],"temp_c":cpu["temp_c"],"load1":cpu["load1"],"load5":cpu["load5"],"load15":cpu["load15"],"cores":cores},"mem":{"percent":mem["percent"],"used":mem["used"],"total":mem["total"],"available":mem["available"],"cached":mem["cached"],"swap_used":mem["swap_used"],"swap_total":mem["swap_total"],"swap_percent":mem["swap_percent"]},"net":{"rx_rate":net["rx_rate"],"tx_rate":net["tx_rate"],"total_rx":net["total_rx"],"total_tx":net["total_tx"]},"disk":{"read_rate":disk["read_rate"],"write_rate":disk["write_rate"],"mounts":mounts},"gpu":{"available":gpu["available"],"gpus":gpus,"processes":gp},"processes":processes,"agents":{"count":agents.len(),"total_tokens":total_tokens,"token_rate":s["token_rate"]}});
    let now = monotonic_ns();
    trace_fd_write(
        &json!({"event":"snapshot_ready","seq":seq,"monotonic_ns":now,"system_collected":true,"gpu_collected":true,"agents_collected":true,"values":values}),
    );
    for panel in [
        "CPU",
        "memory",
        "network",
        "disk",
        "GPU",
        "GPU processes",
        "processes",
        "agents",
        "tokens",
    ] {
        trace_fd_write(
            &json!({"event":"panel_rendered","seq":seq,"panel":panel,"monotonic_ns":monotonic_ns()}),
        );
    }
}
fn ui(theme: &str, _scope: &str) {
    let mut stdout = io::stdout();
    terminal::enable_raw_mode().ok();
    execute!(stdout, terminal::EnterAlternateScreen, cursor::Hide).ok();
    let backend = CrosstermBackend::new(stdout);
    let mut t = Terminal::new(backend).unwrap();
    let mut paused = false;
    let mut view = "system";
    let mut focus = "agents";
    let mut proc_selected = 0usize;
    let mut gpu_selected = 0usize;
    let mut agent_selected = 0usize;
    let mut scope = _scope.to_owned();
    let mut sort_mem = false;
    let mut theme_i = match theme {
        "dracula" => 1,
        "gruvbox" => 2,
        "nord" => 3,
        "adapta" => 4,
        _ => 0,
    };
    let themes = ["btop", "dracula", "gruvbox", "nord", "adapta"];
    let mut current = snapshot(&scope, None);
    let mut tick = 1;
    let mut seq = 0u64;
    let mut fresh = true;
    loop {
        if tick % 2 == 0 && !paused {
            let mut next = snapshot(&scope, None);
            merge_history(&mut next, &current);
            current = next;
            fresh = true;
        }
        let tc = match theme_i {
            1 => Color::Magenta,
            2 => Color::Yellow,
            3 => Color::Cyan,
            4 => Color::Green,
            _ => Color::LightCyan,
        };
        draw(
            &mut t,
            &current,
            view,
            focus,
            proc_selected,
            gpu_selected,
            agent_selected,
            paused,
            tc,
            sort_mem,
        );
        if fresh {
            seq += 1;
            emit_tui_trace(&current, seq);
            fresh = false;
        }
        if event::poll(Duration::from_millis(300)).unwrap_or(false) {
            if let Ok(Event::Key(k)) = event::read() {
                if k.code == KeyCode::Char('q')
                    || k.code == KeyCode::Char('c') && k.modifiers.contains(KeyModifiers::CONTROL)
                {
                    break;
                }
                match k.code {
                    KeyCode::Char('?') => {
                        let _=t.draw(|f|{let a=f.area();f.render_widget(Paragraph::new("EnviTop keys\n\nq quit   ? help   Tab focus   j/k move selection\n1/2/3 views in narrow terminal\np pause/resume   r refresh   t theme   s sort CPU/memory\nx request SIGTERM, then y confirm or n cancel\na toggle agent session scope" ).block(Block::default().title(" Help ").borders(Borders::ALL)),center(a,70,16));});
                        let _ = event::read();
                    }
                    KeyCode::Tab => {
                        if terminal::size().map(|(w, _)| w < 140).unwrap_or(false) {
                            view = match view {
                                "system" => "gpu",
                                "gpu" => "agents",
                                _ => "system",
                            };
                            focus = match view {
                                "gpu" => "gpuprocs",
                                "agents" => "agents",
                                _ => "none",
                            };
                        } else {
                            focus = match focus {
                                "agents" => "gpuprocs",
                                "gpuprocs" => "procs",
                                _ => "agents",
                            }
                        }
                    }
                    KeyCode::BackTab => {
                        if terminal::size().map(|(w, _)| w < 140).unwrap_or(false) {
                            view = match view {
                                "system" => "agents",
                                "agents" => "gpu",
                                _ => "system",
                            };
                            focus = match view {
                                "gpu" => "gpuprocs",
                                "agents" => "agents",
                                _ => "none",
                            };
                        } else {
                            focus = match focus {
                                "agents" => "procs",
                                "procs" => "gpuprocs",
                                _ => "agents",
                            }
                        }
                    }
                    KeyCode::Char('1') => view = "system",
                    KeyCode::Char('2') => view = "gpu",
                    KeyCode::Char('3') => view = "agents",
                    KeyCode::Char('j') | KeyCode::Down => {
                        if focus == "gpuprocs" {
                            gpu_selected = cycle_index(
                                gpu_selected,
                                current["gpu"]["processes"]
                                    .as_array()
                                    .map(Vec::len)
                                    .unwrap_or(0),
                                1,
                            );
                            sync_process_selection(
                                &current,
                                gpu_selected,
                                &mut proc_selected,
                                sort_mem,
                            );
                        } else if focus == "agents" {
                            agent_selected = cycle_index(
                                agent_selected,
                                current["agents"].as_array().map(Vec::len).unwrap_or(0),
                                1,
                            );
                        } else {
                            proc_selected = cycle_index(
                                proc_selected,
                                current["procs"].as_array().map(Vec::len).unwrap_or(0),
                                1,
                            );
                            sync_gpu_selection(
                                &current,
                                proc_selected,
                                &mut gpu_selected,
                                sort_mem,
                            );
                        }
                    }
                    KeyCode::Char('k') | KeyCode::Up => {
                        if focus == "gpuprocs" {
                            gpu_selected = cycle_index(
                                gpu_selected,
                                current["gpu"]["processes"]
                                    .as_array()
                                    .map(Vec::len)
                                    .unwrap_or(0),
                                -1,
                            );
                            sync_process_selection(
                                &current,
                                gpu_selected,
                                &mut proc_selected,
                                sort_mem,
                            );
                        } else if focus == "agents" {
                            agent_selected = cycle_index(
                                agent_selected,
                                current["agents"].as_array().map(Vec::len).unwrap_or(0),
                                -1,
                            );
                        } else {
                            proc_selected = cycle_index(
                                proc_selected,
                                current["procs"].as_array().map(Vec::len).unwrap_or(0),
                                -1,
                            );
                            sync_gpu_selection(
                                &current,
                                proc_selected,
                                &mut gpu_selected,
                                sort_mem,
                            );
                        }
                    }
                    KeyCode::Char('p') => paused = !paused,
                    KeyCode::Char('r') => {
                        let mut next = snapshot(&scope, None);
                        merge_history(&mut next, &current);
                        current = next;
                        fresh = true;
                    }
                    KeyCode::Char('t') => theme_i = (theme_i + 1) % themes.len(),
                    KeyCode::Char('s') => sort_mem = !sort_mem,
                    KeyCode::Char('a') => {
                        scope = if scope == "recent" { "all" } else { "recent" }.to_owned();
                        let mut next = snapshot(&scope, None);
                        merge_history(&mut next, &current);
                        current = next;
                        fresh = true;
                    }
                    KeyCode::Char('x') => {
                        let target = match focus {
                            "gpuprocs" => current["gpu"]["processes"]
                                .as_array()
                                .and_then(|x| x.get(gpu_selected))
                                .map(|p| {
                                    (
                                        p["pid"].as_i64().unwrap_or(0),
                                        p["name"].as_str().unwrap_or("GPU process").to_owned(),
                                    )
                                }),
                            "agents" => current["agents"]
                                .as_array()
                                .and_then(|x| x.get(agent_selected))
                                .filter(|a| a["agent"] != "opencode")
                                .map(|a| {
                                    (
                                        a["pid"].as_i64().unwrap_or(0),
                                        a["agent"].as_str().unwrap_or("agent").to_owned(),
                                    )
                                }),
                            _ => sorted_processes(&current, sort_mem)
                                .get(proc_selected)
                                .map(|p| {
                                    (
                                        p["pid"].as_i64().unwrap_or(0),
                                        p["name"].as_str().unwrap_or("process").to_owned(),
                                    )
                                }),
                        };
                        if let Some((pid, name)) =
                            target.filter(|(pid, _)| *pid > 1 && *pid != std::process::id() as i64)
                        {
                            let _ = t.draw(|f| {
                                let a = f.area();
                                f.render_widget(
                                    Paragraph::new(format!("Terminate {name} (PID {pid})?  y / n"))
                                        .block(
                                            Block::default()
                                                .title(" Confirm ")
                                                .borders(Borders::ALL),
                                        ),
                                    center(a, 60, 5),
                                );
                            });
                            let confirm = event::read().ok().and_then(|e| {
                                if let Event::Key(k) = e {
                                    Some(k.code)
                                } else {
                                    None
                                }
                            });
                            if confirm == Some(KeyCode::Char('y')) {
                                unsafe {
                                    libc::kill(pid as i32, libc::SIGTERM);
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        tick += 1
    }
    execute!(
        t.backend_mut(),
        terminal::LeaveAlternateScreen,
        cursor::Show
    )
    .ok();
    terminal::disable_raw_mode().ok();
}
fn center(r: Rect, w: u16, h: u16) -> Rect {
    let x = r.width.saturating_sub(w) / 2;
    let y = r.height.saturating_sub(h) / 2;
    Rect::new(r.x + x, r.y + y, w.min(r.width), h.min(r.height))
}
fn clock_text() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as libc::time_t;
    let mut local: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&seconds, &mut local) }.is_null() {
        return "--:--:--".into();
    }
    format!(
        "{:02}:{:02}:{:02}",
        local.tm_hour, local.tm_min, local.tm_sec
    )
}
fn cycle_index(current: usize, len: usize, delta: isize) -> usize {
    if len == 0 {
        return 0;
    }
    ((current as isize + delta).rem_euclid(len as isize)) as usize
}
fn sync_process_selection(s: &Value, gpu_index: usize, process_index: &mut usize, sort_mem: bool) {
    if let Some(pid) = s["gpu"]["processes"]
        .as_array()
        .and_then(|x| x.get(gpu_index))
        .and_then(|x| x["pid"].as_i64())
    {
        let rows = sorted_processes(s, sort_mem);
        if let Some(i) = rows.iter().position(|p| p["pid"].as_i64() == Some(pid)) {
            *process_index = i;
        }
    }
}
fn sync_gpu_selection(s: &Value, process_index: usize, gpu_index: &mut usize, sort_mem: bool) {
    if let Some(pid) = sorted_processes(s, sort_mem)
        .get(process_index)
        .and_then(|x| x["pid"].as_i64())
    {
        if let Some(i) = s["gpu"]["processes"]
            .as_array()
            .and_then(|x| x.iter().position(|p| p["pid"].as_i64() == Some(pid)))
        {
            *gpu_index = i;
        }
    }
}
fn sorted_processes(s: &Value, sort_mem: bool) -> Vec<Value> {
    let mut rows = s["procs"].as_array().cloned().unwrap_or_default();
    if sort_mem {
        rows.sort_by(|a, b| b["rss"].as_u64().cmp(&a["rss"].as_u64()));
    }
    rows
}
fn draw(
    t: &mut Terminal<CrosstermBackend<io::Stdout>>,
    s: &Value,
    view: &str,
    focus: &str,
    proc_selected: usize,
    gpu_selected: usize,
    agent_selected: usize,
    paused: bool,
    accent: Color,
    sort_mem: bool,
) {
    let _=t.draw(|f|{
        let area=f.area();
        let rows=Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(2),Constraint::Min(8),Constraint::Length(1)]).split(area);
        f.render_widget(Paragraph::new(format!(" EnviTop  {} · {} · up {:.1}h · {} · clock {}",s["hostname"].as_str().unwrap_or("host"),s["username"].as_str().unwrap_or("user"),s["uptime"].as_f64().unwrap_or(0.0)/3600.0,s["os_name"].as_str().unwrap_or("Linux"),clock_text())).style(Style::default().fg(accent).add_modifier(Modifier::BOLD)),rows[0]);
        let narrow=area.width<140;
        let visible=if narrow {match view{"gpu"=>"gpu","agents"=>"agents",_=>"system"}}else{"all"};
        let col=if visible=="gpu"{vec![rows[1]]}else if visible=="agents"{vec![rows[1]]}else if narrow{vec![rows[1]]}else{Layout::default().direction(Direction::Horizontal).constraints([Constraint::Percentage(48),Constraint::Percentage(52)]).split(rows[1]).to_vec()};
        let cpu=&s["cpu"];let mem=&s["mem"];let net=&s["net"];let disk=&s["disk"];let gpu=&s["gpu"];
        if visible=="all"||visible=="system"{
            let l=col[0];let blocks=Layout::default().direction(Direction::Vertical).constraints([Constraint::Percentage(29),Constraint::Percentage(20),Constraint::Percentage(19),Constraint::Percentage(32)]).split(l);
            let cb=Block::default().title(" CPU ").borders(Borders::ALL);let ci=cb.inner(blocks[0]);f.render_widget(cb,blocks[0]);let cp=Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(1),Constraint::Length(1),Constraint::Min(1)]).split(ci);
            f.render_widget(Paragraph::new(format!("{:.1}%  {:.2}GHz  {}  load {:.2} {:.2} {:.2}",num(cpu,"percent"),num(cpu,"freq_mhz")/1000.0,cpu["temp_c"].as_f64().map(|x|format!("{x:.0}°C")).unwrap_or_else(||"—°C".into()),num(cpu,"load1"),num(cpu,"load5"),num(cpu,"load15"))),cp[0]);
            let mut coreline=String::new();if let Some(cs)=cpu["per_core"].as_array(){for(i,c)in cs.iter().enumerate(){if i>0{coreline.push_str("  ")}let temp=cpu["core_temps"].get(i).and_then(Value::as_f64).map(|x|format!("{x:.0}°C")).unwrap_or_else(||"—°C".into());coreline.push_str(&format!("Core {i} {:.0}% {temp}",c.as_f64().unwrap_or(0.0)));}}f.render_widget(Paragraph::new(coreline),cp[1]);
            let cv=cpu["history"].as_array().map(|x|x.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();let uv=cpu["user_history"].as_array().map(|x|x.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();let cv=if cv.is_empty(){vec![num(cpu,"percent")]}else{cv};let uv=if uv.is_empty(){vec![num(cpu,"user_percent")]}else{uv};let chart=braille_chart(&cv,Some(&uv),cp[2].width.saturating_sub(2)as usize,cp[2].height.saturating_sub(2)as usize,100.0);f.render_widget(Paragraph::new(chart).block(Block::default().title(" CPU graph ").borders(Borders::ALL)),cp[2]);
            let mb=Block::default().title(" Memory ").borders(Borders::ALL);let mi=mb.inner(blocks[1]);f.render_widget(mb,blocks[1]);let mp=Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(2),Constraint::Min(1)]).split(mi);f.render_widget(Paragraph::new(format!("RAM {:.1}%  {}/{}  avail {}  cached {}\nSWAP {}/{} ({:.1}%)",num(mem,"percent"),ibytes(mem["used"].as_u64().unwrap_or(0)),ibytes(mem["total"].as_u64().unwrap_or(0)),ibytes(mem["available"].as_u64().unwrap_or(0)),ibytes(mem["cached"].as_u64().unwrap_or(0)),ibytes(mem["swap_used"].as_u64().unwrap_or(0)),ibytes(mem["swap_total"].as_u64().unwrap_or(0)),num(mem,"swap_percent"))),mp[0]);
            let mh=mem["history"].as_array().map(|x|x.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();let mh=if mh.is_empty(){vec![num(mem,"percent")]}else{mh};let chart=braille_chart(&mh,None,mp[1].width.saturating_sub(2)as usize,mp[1].height.saturating_sub(2)as usize,100.0);f.render_widget(Paragraph::new(chart).block(Block::default().title(" RAM graph ").borders(Borders::ALL)),mp[1]);
            let nb=Block::default().title(" Network ").borders(Borders::ALL);let ni=nb.inner(blocks[2]);f.render_widget(nb,blocks[2]);let np=Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(1),Constraint::Min(1)]).split(ni);f.render_widget(Paragraph::new(format!("RX ↓ {}  TX ↑ {}",rate(num(net,"rx_rate")),rate(num(net,"tx_rate")))),np[0]);
            let rxh=net["rx_history"].as_array().map(|x|x.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();let txh=net["tx_history"].as_array().map(|x|x.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();let peak=rxh.iter().chain(txh.iter()).copied().fold(1.0,f64::max);let chart=braille_chart(&rxh,Some(&txh),np[1].width.saturating_sub(2)as usize,np[1].height.saturating_sub(2)as usize,peak);f.render_widget(Paragraph::new(chart).block(Block::default().title(" Network graph ").borders(Borders::ALL)),np[1]);
            let db=Block::default().title(" Disk ").borders(Borders::ALL);let di=db.inner(blocks[3]);f.render_widget(db,blocks[3]);let dp=Layout::default().direction(Direction::Vertical).constraints([Constraint::Length(1),Constraint::Percentage(32),Constraint::Min(1)]).split(di);f.render_widget(Paragraph::new(format!("R {}  W {}",rate(num(disk,"read_rate")),rate(num(disk,"write_rate")))),dp[0]);let rh=disk["read_history"].as_array().map(|x|x.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();let wh=disk["write_history"].as_array().map(|x|x.iter().filter_map(Value::as_f64).collect::<Vec<_>>()).unwrap_or_default();let peak=rh.iter().chain(wh.iter()).copied().fold(1.0,f64::max);let chart=braille_chart(&rh,Some(&wh),dp[1].width.saturating_sub(2)as usize,dp[1].height.saturating_sub(2)as usize,peak);f.render_widget(Paragraph::new(chart).block(Block::default().title(" Disk IO graph ").borders(Borders::ALL)),dp[1]);let mut dtext=String::new();for m in disk["mounts"].as_array().into_iter().flatten().take(5){dtext.push_str(&format!("{:<16} {} / {}  free {}  R {} W {}\n",m["mount"].as_str().unwrap_or("?"),ibytes(m["used"].as_u64().unwrap_or(0)),ibytes(m["total"].as_u64().unwrap_or(0)),ibytes(m["free"].as_u64().unwrap_or(0)),rate(num(m,"read_rate")),rate(num(m,"write_rate"))))}f.render_widget(Paragraph::new(dtext).wrap(Wrap{trim:true}),dp[2]);
        }
        if visible=="all"||visible=="gpu"||visible=="agents"{
            let r=if visible=="gpu"||visible=="agents"{col[0]}else{col[1]};let rb=Block::default().borders(Borders::NONE);let ri=rb.inner(r);f.render_widget(rb,r);let split=Layout::default().direction(Direction::Vertical).constraints([Constraint::Percentage(24),Constraint::Percentage(15),Constraint::Percentage(26),Constraint::Percentage(18),Constraint::Percentage(17)]).split(ri);
            let mut gt=String::new();if gpu["available"].as_bool().unwrap_or(false){for (i,g) in gpu["gpus"].as_array().into_iter().flatten().enumerate(){gt.push_str(&format!("GPU{i} {}  {:.0}%  {}°C\nVRAM {:.1}%  {} / {}\n",g["name"].as_str().unwrap_or("GPU"),num(g,"util"),num(g,"temp_c"),num(g,"mem_percent"),ibytes(g["mem_used"].as_u64().unwrap_or(0)),ibytes(g["mem_total"].as_u64().unwrap_or(0))))}}else{gt.push_str("NVIDIA GPU unavailable\n")};gt.push_str(&format!("Driver {}  CUDA {}",gpu["driver"].as_str().unwrap_or("—"),gpu["cuda"].as_str().unwrap_or("—")));f.render_widget(Paragraph::new(gt).block(Block::default().title(" GPU ").borders(Borders::ALL)),split[0]);
            let gps=s["gpu"]["processes"].as_array().cloned().unwrap_or_default();let gr=gps.iter().enumerate().map(|(i,p)|Row::new(vec![Cell::from((if i==gpu_selected&&focus=="gpuprocs"{"▶"}else{" "}).to_owned()),Cell::from(p["pid"].to_string()),Cell::from(p["sm"].as_f64().map(|x|format!("{x:.0}")).unwrap_or_else(||"—".into())),Cell::from(p["gmbw_pct"].as_f64().map(|x|format!("{x:.0}")).unwrap_or_else(||"—".into())),Cell::from(p["vram"].as_u64().map(ibytes).unwrap_or_else(||"—".into())),Cell::from(p["runtime_s"].as_f64().map(|x|format!("{:02}:{:02}:{:02}",(x as u64)/3600,((x as u64)%3600)/60,(x as u64)%60)).unwrap_or_else(||"—".into())),Cell::from(p["name"].as_str().unwrap_or("").to_owned())]));f.render_widget(Table::new(gr,[Constraint::Length(2),Constraint::Length(8),Constraint::Length(5),Constraint::Length(6),Constraint::Length(9),Constraint::Length(10),Constraint::Min(8)]).header(Row::new(["","PID","SM%","GMBW%","VRAM","TIME","PROCESS"])).block(Block::default().title(" GPU processes ").borders(Borders::ALL)),split[1]);
            let mut ps=s["procs"].as_array().cloned().unwrap_or_default();if sort_mem{ps.sort_by(|a,b|b["rss"].as_u64().cmp(&a["rss"].as_u64()))}let pr=ps.iter().take(8).enumerate().map(|(i,p)|Row::new(vec![Cell::from((if i==proc_selected&&focus=="procs"{"▶"}else{" "}).to_owned()),Cell::from(p["pid"].to_string()),Cell::from(format!("{:.1}",num(p,"cpu"))),Cell::from(format!("{:.1}",num(p,"mem_pct"))),Cell::from(p["rss"].as_u64().map(ibytes).unwrap_or_default()),Cell::from(p["name"].as_str().unwrap_or("").to_owned())]));f.render_widget(Table::new(pr,[Constraint::Length(2),Constraint::Length(8),Constraint::Length(6),Constraint::Length(6),Constraint::Length(9),Constraint::Min(8)]).header(Row::new(["","PID","CPU%","MEM%","RSS","PROCESS"])).block(Block::default().title(" Processes ").borders(Borders::ALL)),split[2]);
            let agents=s["agents"].as_array().cloned().unwrap_or_default();let count=agents.len();let total:u64=agents.iter().map(|x|x["total_tokens"].as_u64().unwrap_or(0)).sum();let mut at=format!("Agent overview: {count} sessions  tokens {}  rate {}\n",human_tokens(total),format!("{:.1} tokens/s",num(s,"token_rate")));for (i,a) in agents.iter().enumerate().skip(agent_selected.saturating_sub(1)).take(7){at.push_str(&format!("{} {} {}  {}  {} tokens\n",if i==agent_selected&&focus=="agents"{"▶"}else{" "},a["agent"].as_str().unwrap_or(""),a["project"].as_str().unwrap_or(""),a["status"].as_str().unwrap_or(""),human_tokens(a["total_tokens"].as_u64().unwrap_or(0))))}f.render_widget(Paragraph::new(at).wrap(Wrap{trim:true}).block(Block::default().title(" Agents ").borders(Borders::ALL)),split[3]);let tokens=Paragraph::new(format!("Total tokens {}  rate {}\nSessions {}  input {}  output {}\nCache read {}  cache write {}",human_tokens(total),format!("{:.1} tokens/s",num(s,"token_rate")),count,human_tokens(agents.iter().map(|a|a["input_tokens"].as_u64().unwrap_or(0)).sum()),human_tokens(agents.iter().map(|a|a["output_tokens"].as_u64().unwrap_or(0)).sum()),human_tokens(agents.iter().map(|a|a["cache_read"].as_u64().unwrap_or(0)).sum()),human_tokens(agents.iter().map(|a|a["cache_write"].as_u64().unwrap_or(0)).sum()))).wrap(Wrap{trim:true}).block(Block::default().title(" Tokens ").borders(Borders::ALL));f.render_widget(tokens,split[4]);
        }
        f.render_widget(Paragraph::new(format!("Tab focus:{}  j/k move  1/2/3 view:{}  p pause:{}  r refresh  t theme  s sort  x kill  a scope:{}  ? help  q quit",focus,view,paused,s["agent_scope"].as_str().unwrap_or("recent"))),rows[2]);
    });
}
fn human_tokens(v: u64) -> String {
    if v >= 1_000_000 {
        format!("{:.1}M", v as f64 / 1_000_000.0)
    } else if v >= 1000 {
        format!("{:.1}k", v as f64 / 1000.0)
    } else {
        v.to_string()
    }
}

fn braille_chart(
    a: &[f64],
    b: Option<&[f64]>,
    width: usize,
    height: usize,
    scale: f64,
) -> Vec<Line<'static>> {
    if width == 0 || height == 0 {
        return Vec::new();
    }
    let mut masks = vec![vec![0u8; width]; height];
    let mut series = vec![vec![0u8; width]; height];
    let x_total = width * 2;
    let y_total = height * 4;
    let plot = |values: &[f64], bitset: u8, masks: &mut Vec<Vec<u8>>, series: &mut Vec<Vec<u8>>| {
        if values.is_empty() {
            return;
        }
        for (i, v) in values.iter().enumerate() {
            let x = if values.len() == 1 {
                x_total - 1
            } else {
                i * (x_total - 1) / (values.len() - 1)
            };
            let up = (v.max(0.0).min(scale.max(0.01)) / scale.max(0.01)
                * (y_total.saturating_sub(1)) as f64)
                .round() as usize;
            let y = y_total - 1 - up;
            let col = x / 2;
            let row = y % 4;
            let mask = match (x % 2, row) {
                (0, 0) => 1,
                (0, 1) => 2,
                (0, 2) => 4,
                (0, 3) => 64,
                (1, 0) => 8,
                (1, 1) => 16,
                (1, 2) => 32,
                _ => 128,
            };
            masks[y / 4][col] |= mask;
            series[y / 4][col] |= bitset;
        }
    };
    plot(a, 1, &mut masks, &mut series);
    if let Some(v) = b {
        plot(v, 2, &mut masks, &mut series)
    }
    masks
        .into_iter()
        .zip(series)
        .map(|(row, owners)| {
            Line::from(
                row.into_iter()
                    .zip(owners)
                    .map(|(m, o)| {
                        let ch = char::from_u32(0x2800 + m as u32).unwrap_or(' ');
                        let color = match o {
                            1 => Color::Green,
                            2 => Color::Cyan,
                            3 => Color::White,
                            _ => Color::Reset,
                        };
                        Span::styled(ch.to_string(), Style::default().fg(color))
                    })
                    .collect::<Vec<_>>(),
            )
        })
        .collect()
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.iter().any(|x| x == "--version" || x == "-V") {
        println!("envitop 0.1.0");
        return;
    }
    if args.iter().any(|x| x == "--help" || x == "-h") {
        println!("EnviTop system, NVIDIA GPU and agent monitor\n\nUsage: envitop [--once] [--json] [--scope recent|all] [--theme btop|dracula|gruvbox|nord|adapta]\n\nKeys: q quit, ? help, Tab focus/view, j/k select, p pause, r refresh, t theme, s sort, x kill, a scope");
        return;
    }
    let once = args.iter().any(|x| x == "--once" || x == "--json");
    let jsonflag = args.iter().any(|x| x == "--json");
    if once {
        let scope = args
            .iter()
            .position(|x| x == "--scope")
            .and_then(|i| args.get(i + 1))
            .map(String::as_str)
            .unwrap_or("recent");
        json_mode(scope, args.iter().any(|x| x == "--profile-json"));
        return;
    }
    let theme = args
        .iter()
        .position(|x| x == "--theme")
        .and_then(|i| args.get(i + 1))
        .map(String::as_str)
        .unwrap_or("btop");
    let scope = args
        .iter()
        .position(|x| x == "--scope")
        .and_then(|i| args.get(i + 1))
        .map(String::as_str)
        .unwrap_or("recent");
    if jsonflag {
        json_mode(scope, args.iter().any(|x| x == "--profile-json"))
    } else {
        ui(theme, scope)
    }
}
