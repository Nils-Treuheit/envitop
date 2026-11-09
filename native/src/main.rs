use libc::{poll, pollfd, termios, tcgetattr, tcsetattr, TCSANOW, ECHO, ICANON, ISIG, VMIN, VTIME};
use serde_json::Value;
use std::collections::{HashMap, VecDeque};
use std::fs;
use std::io::{self, BufRead, Read, Write};
use std::path::Path;
use std::ffi::CString;
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::{Duration, Instant};
use std::os::unix::fs::MetadataExt;

const HISTORY: usize = 120;
static LAST_GPU_PROCESS_ROWS: AtomicUsize = AtomicUsize::new(0);

#[derive(Default, Clone)]
struct CpuTimes { total: u64, user: u64, idle: u64 }
#[derive(Default, Clone)]
struct Proc { pid: i32, user: String, cpu: f64, rss_kb: u64, cmd: String }
#[derive(Default, Clone)]
struct Gpu { name: String, util: f64, used: u64, total: u64, temp: f64, power: f64, procs: Vec<String> }
#[derive(Default, Clone)]
struct Mount { path: String, used: u64, total: u64, free: u64, read: f64, write: f64, read_hist: VecDeque<f64>, write_hist: VecDeque<f64> }
#[derive(Default, Clone)]
struct Data {
    host: String, model: String, uptime: u64, cpu: f64, user_cpu: f64, cores: Vec<f64>, core_temps: Vec<Option<f64>>, temp: f64,
    load: String, mem_used: u64, mem_total: u64, mem_avail: u64, mem_cached: u64,
    swap_used: u64, swap_total: u64, rx: f64, tx: f64, read: f64, write: f64,
    mounts: Vec<Mount>, procs: Vec<Proc>, gpu: Gpu,
    cpu_hist: VecDeque<f64>, user_hist: VecDeque<f64>, mem_hist: VecDeque<f64>, swap_hist: VecDeque<f64>,
    rx_hist: VecDeque<f64>, tx_hist: VecDeque<f64>, read_hist: VecDeque<f64>, write_hist: VecDeque<f64>,
    gpu_hist: VecDeque<f64>, gpu_mem_hist: VecDeque<f64>,
}

fn read_cpu() -> (CpuTimes, Vec<CpuTimes>) {
    let text = fs::read_to_string("/proc/stat").unwrap_or_default();
    let mut lines = text.lines();
    let mut total = CpuTimes::default();
    let mut cores = Vec::new();
    for (i, line) in std::iter::once(lines.next().unwrap_or("")).chain(lines).enumerate() {
        let mut it = line.split_whitespace();
        let tag = it.next().unwrap_or("");
        if tag != "cpu" && !(tag.starts_with("cpu") && tag[3..].parse::<usize>().is_ok()) { continue; }
        let ticks: Vec<u64> = it.map(|v| v.parse().unwrap_or(0)).collect();
        let all: u64 = ticks.iter().take(8).sum();
        let user = ticks.first().copied().unwrap_or(0) + ticks.get(1).copied().unwrap_or(0);
        let idle = ticks.get(3).copied().unwrap_or(0) + ticks.get(4).copied().unwrap_or(0);
        if tag == "cpu" { total = CpuTimes { total: all, user, idle }; }
        else if i > 0 { cores.push(CpuTimes { total: all, user, idle }); }
    }
    (total, cores)
}

fn rates(prev: u64, current: u64, elapsed: f64) -> f64 {
    current.saturating_sub(prev) as f64 / elapsed.max(0.001)
}

fn cpu_delta(previous: &CpuTimes, current: &CpuTimes) -> (f64, f64) {
    let dt = current.total.saturating_sub(previous.total).max(1) as f64;
    let idle = current.idle.saturating_sub(previous.idle) as f64;
    (100.0 * (dt - idle).max(0.0) / dt,
     100.0 * current.user.saturating_sub(previous.user) as f64 / dt)
}

fn read_mem(d: &mut Data) {
    let mut vals = HashMap::new();
    let content = fs::read_to_string("/proc/meminfo").unwrap_or_default();
    for line in content.lines() {
        if let Some((key, value)) = line.split_once(':') {
            let kb = value.split_whitespace().next().unwrap_or("0").parse::<u64>().unwrap_or(0);
            vals.insert(key, kb * 1024);
        }
    }
    d.mem_total = *vals.get("MemTotal").unwrap_or(&0);
    d.mem_avail = *vals.get("MemAvailable").unwrap_or(&0);
    d.mem_used = d.mem_total.saturating_sub(d.mem_avail);
    d.mem_cached = vals.get("Cached").copied().unwrap_or(0) + vals.get("SReclaimable").copied().unwrap_or(0);
    d.swap_total = *vals.get("SwapTotal").unwrap_or(&0);
    d.swap_used = d.swap_total.saturating_sub(*vals.get("SwapFree").unwrap_or(&0));
}

fn read_network() -> (u64, u64) {
    let mut rx = 0; let mut tx = 0;
    for line in fs::read_to_string("/proc/net/dev").unwrap_or_default().lines().skip(2) {
        if let Some((name, values)) = line.split_once(':') {
            if name.trim() == "lo" { continue; }
            let v: Vec<u64> = values.split_whitespace().map(|s| s.parse().unwrap_or(0)).collect();
            rx += v.first().copied().unwrap_or(0); tx += v.get(8).copied().unwrap_or(0);
        }
    }
    (rx, tx)
}

fn read_disks() -> (u64, u64) {
    let mut read = 0; let mut write = 0;
    for line in fs::read_to_string("/proc/diskstats").unwrap_or_default().lines() {
        let v: Vec<&str> = line.split_whitespace().collect();
        if v.len() < 14 { continue; }
        let dev = v[2];
        if dev.starts_with("loop") || dev.starts_with("ram") || !Path::new(&format!("/sys/block/{dev}")).exists() { continue; }
        read += v[5].parse::<u64>().unwrap_or(0) * 512;
        write += v[9].parse::<u64>().unwrap_or(0) * 512;
    }
    (read, write)
}

fn read_disk_devices() -> HashMap<String, (u64, u64)> {
    let mut result = HashMap::new();
    for line in fs::read_to_string("/proc/diskstats").unwrap_or_default().lines() {
        let v: Vec<&str> = line.split_whitespace().collect();
        if v.len() < 14 { continue; }
        let name = v[2];
        result.insert(name.to_owned(), (v[5].parse::<u64>().unwrap_or(0) * 512, v[9].parse::<u64>().unwrap_or(0) * 512));
    }
    result
}

fn unescape_mount(s: &str) -> String {
    s.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
}

fn mount_device_name(source: &str, devices: &HashMap<(u64, u64), String>) -> String {
    if let Ok(metadata) = fs::metadata(source) {
        let device = metadata.rdev();
        let major = libc::major(device) as u64;
        let minor = libc::minor(device) as u64;
        if let Some(name) = devices.get(&(major, minor)) { return name.clone(); }
    }
    source.rsplit('/').next().unwrap_or("").to_owned()
}

fn read_mounts(previous: &HashMap<String, (u64, u64)>, elapsed: f64) -> Vec<Mount> {
    let mut seen = std::collections::HashSet::new();
    let mut mounts = Vec::new();
    let current = read_disk_devices();
    let devices: HashMap<(u64, u64), String> = fs::read_to_string("/proc/diskstats").unwrap_or_default().lines()
        .filter_map(|line| {
            let v: Vec<&str> = line.split_whitespace().collect();
            if v.len() < 3 { return None; }
            Some(((v[0].parse().ok()?, v[1].parse().ok()?), v[2].to_owned()))
        }).collect();
    for line in fs::read_to_string("/proc/mounts").unwrap_or_default().lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() < 3 || !fields[0].starts_with("/dev/") { continue; }
        let path = unescape_mount(fields[1]);
        if !seen.insert(path.clone()) { continue; }
        let Ok(cpath) = CString::new(path.as_bytes()) else { continue; };
        let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(cpath.as_ptr(), &mut stat) } != 0 { continue; }
        let unit = if stat.f_frsize > 0 { stat.f_frsize as u64 } else { stat.f_bsize as u64 };
        let total = stat.f_blocks as u64 * unit;
        let free = stat.f_bavail as u64 * unit;
        let dev = mount_device_name(fields[0], &devices);
        let (r, w) = current.get(&dev).copied().unwrap_or((0, 0));
        let (old_r, old_w) = previous.get(&dev).copied().unwrap_or((r, w));
        mounts.push(Mount {
            path, used: total.saturating_sub(free), total, free,
            read: rates(old_r, r, elapsed), write: rates(old_w, w, elapsed),
            ..Mount::default()
        });
    }
    mounts.sort_by(|a,b| b.total.cmp(&a.total).then_with(|| a.path.cmp(&b.path)));
    mounts
}

fn cpu_model() -> String {
    fs::read_to_string("/proc/cpuinfo").unwrap_or_default().lines()
        .find_map(|l| l.strip_prefix("model name\t: ").or_else(|| l.strip_prefix("Hardware\t: ")))
        .unwrap_or("CPU").trim().to_owned()
}

fn cpu_temps(core_count: usize) -> (f64, Vec<Option<f64>>) {
    let mut package = 0.0f64;
    let mut per_core = vec![None; core_count];
    if let Ok(devices) = fs::read_dir("/sys/class/hwmon") {
        for dev in devices.flatten() {
            let Ok(files) = fs::read_dir(dev.path()) else { continue; };
            let paths: Vec<_> = files.flatten().collect();
            for f in &paths {
                let n=f.file_name().to_string_lossy().to_string();
                if !(n.starts_with("temp") && n.ends_with("_input")) {continue;}
                let v=fs::read_to_string(f.path()).unwrap_or_default().trim().parse::<f64>().unwrap_or(0.0)/1000.0;
                if v<=0.0 {continue;}
                let label_name=n.trim_end_matches("_input").to_owned()+"_label";
                let label=paths.iter().find(|p|p.file_name().to_string_lossy()==label_name)
                    .and_then(|p|fs::read_to_string(p.path()).ok()).unwrap_or_default().trim().to_lowercase();
                if label.contains("tctl") || label.contains("package") || label.contains("cpu") {package=package.max(v);}
                if let Some(index)=label.strip_prefix("core ").and_then(|s|s.parse::<usize>().ok()) {
                    if index<core_count {per_core[index]=Some(v);}
                }
                if label.contains("tccd") {
                    let chiplet=label.chars().rev().find(|c|c.is_ascii_digit()).and_then(|c|c.to_digit(10)).unwrap_or(1) as usize;
                    let halves=(core_count+1)/2;
                    let (start,end)=if chiplet<=1 {(0,halves)}else{(halves,core_count)};
                    for t in per_core.iter_mut().take(end).skip(start){*t=Some(t.unwrap_or(0.0).max(v));}
                }
            }
        }
    }
    (package,per_core)
}

fn process_snapshot(old: &HashMap<i32, u64>, elapsed: f64) -> (Vec<Proc>, HashMap<i32, u64>) {
    let mut rows = Vec::new(); let mut next = HashMap::new();
    let Ok(entries) = fs::read_dir("/proc") else { return (rows, next) };
    let ticks_per_second = 100.0;
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i32>() else { continue };
        let stat = fs::read_to_string(entry.path().join("stat")).unwrap_or_default();
        let Some(end) = stat.rfind(')') else { continue };
        let fields: Vec<&str> = stat[end + 1..].split_whitespace().collect();
        if fields.len() < 22 { continue; }
        let ticks = fields[11].parse::<u64>().unwrap_or(0) + fields[12].parse::<u64>().unwrap_or(0);
        next.insert(pid, ticks);
        let cpu = old.get(&pid).map(|p| ticks.saturating_sub(*p) as f64 / ticks_per_second / elapsed.max(0.001) * 100.0).unwrap_or(0.0);
        let rss_pages = fields[21].parse::<u64>().unwrap_or(0);
        let rss_kb = rss_pages * 4;
        let cmdline = fs::read(entry.path().join("cmdline")).unwrap_or_default();
        let command = String::from_utf8_lossy(&cmdline).replace('\0', " ").trim().to_owned();
        let name = stat[..end].trim_start_matches('(').to_owned();
        let user = entry.metadata().ok().map(|m| format!("{}", m.uid())).unwrap_or_default();
        rows.push(Proc { pid, user, cpu, rss_kb, cmd: if command.is_empty() { name } else { command } });
    }
    rows.sort_by(|a,b| b.cpu.total_cmp(&a.cpu)); rows.truncate(30);
    (rows, next)
}

fn gpu_thread() -> Receiver<Gpu> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut last = Gpu::default();
        let mut empty_process_samples = 0u8;
        let mut unavailable_samples = 0u8;
        loop {
            let mut sample_ok = false;
            let output = Command::new("nvidia-smi")
                .args(["--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"])
                .stdout(Stdio::piped()).stderr(Stdio::null()).output();
            if let Ok(out) = output {
                if out.status.success() {
                    if let Some(line) = String::from_utf8_lossy(&out.stdout).lines().next() {
                        let p: Vec<&str> = line.split(',').map(str::trim).collect();
                        if p.len() >= 6 && !p[0].is_empty() {
                            sample_ok = true;
                            unavailable_samples = 0;
                            last.name=p[0].to_owned(); last.util=p[1].parse().unwrap_or(last.util);
                            last.used=p[2].parse::<u64>().map(|v|v*1024*1024).unwrap_or(last.used);
                            last.total=p[3].parse::<u64>().map(|v|v*1024*1024).unwrap_or(last.total);
                            last.temp=p[4].parse().unwrap_or(last.temp); last.power=p[5].parse().unwrap_or(last.power);
                            if let Some(processes)=sample_gpu_processes() {
                                if processes.is_empty() && !last.procs.is_empty() {
                                    empty_process_samples=empty_process_samples.saturating_add(1);
                                    if empty_process_samples >= 2 { last.procs.clear(); empty_process_samples=0; }
                                } else { last.procs=processes; empty_process_samples=0; }
                            }
                        }
                    }
                }
            }
            if sample_ok {
                if tx.send(last.clone()).is_err() { break; }
            } else {
                unavailable_samples=unavailable_samples.saturating_add(1);
                if unavailable_samples>=2 {
                    last=Gpu::default(); empty_process_samples=0; unavailable_samples=0;
                    if tx.send(last.clone()).is_err() { break; }
                }
            }
            thread::sleep(Duration::from_secs(1));
        }
    });
    rx
}

fn sample_gpu_processes() -> Option<Vec<String>> {
    let pmon=Command::new("nvidia-smi").args(["pmon","-c","1"]).stdout(Stdio::piped()).stderr(Stdio::null()).output().ok()?;
    if !pmon.status.success() { return None; }
    let apps=Command::new("nvidia-smi").args(["--query-compute-apps=pid,process_name,used_memory","--format=csv,noheader,nounits"]).output().ok()?;
    if !apps.status.success() { return None; }
    let mut memory=HashMap::new();
    for line in String::from_utf8_lossy(&apps.stdout).lines() {
        let p:Vec<&str>=line.split(',').map(str::trim).collect();
        if p.len()>=3 { if let (Ok(pid),Ok(mib))=(p[0].parse::<u32>(),p[2].parse::<u64>()) {memory.insert(pid,mib);} }
    }
    let mut rows=Vec::new();
    for line in String::from_utf8_lossy(&pmon.stdout).lines() {
        if line.trim().is_empty() || line.trim_start().starts_with('#') {continue;}
        let p:Vec<&str>=line.split_whitespace().collect();
        if p.len()<3 {continue;}
        let (Ok(gpu),Ok(pid))=(p[0].parse::<u32>(),p[1].parse::<u32>()) else {continue;};
        if pid==0 {continue;}
        let sm=p.get(3).copied().unwrap_or("-"); let mem=p.get(4).copied().unwrap_or("-");
        let command=fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_else(|_|p.get(9).copied().unwrap_or("?").to_owned());
        let vram=memory.get(&pid).map(|m|format!("{m}MiB")).unwrap_or_else(||"—".to_owned());
        rows.push((gpu,pid,memory.get(&pid).copied().unwrap_or(0),format!("{pid:>7} {gpu:>2} {} {:>3}% {:>3}% {:>7} {}",p[2],sm,mem,vram,command.trim())));
    }
    rows.sort_by_key(|(gpu,pid,mem,_)|(*gpu,std::cmp::Reverse(*mem),*pid));
    if rows.is_empty() {
        for line in String::from_utf8_lossy(&apps.stdout).lines() {
            let p:Vec<&str>=line.split(',').map(str::trim).collect();
            if p.len()>=3 {
                if let (Ok(pid),Ok(mib))=(p[0].parse::<u32>(),p[2].parse::<u64>()) {
                    let name=fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_else(|_|p[1].to_owned());
                    rows.push((0,pid,mib,format!("{pid:>7}  0 C   —   — {:>7}MiB {}",mib,name.trim())));
                }
            }
        }
    }
    Some(rows.into_iter().take(12).map(|(_,_,_,line)|line).collect())
}

fn push(hist: &mut VecDeque<f64>, v: f64) { hist.push_back(v); if hist.len() > HISTORY { hist.pop_front(); } }
fn smooth(values: &[f64]) -> Vec<f64> {
    values.iter().enumerate().map(|(i,_)| {
        let start=i.saturating_sub(1); let end=(i+2).min(values.len());
        values[start..end].iter().sum::<f64>()/(end-start).max(1) as f64
    }).collect()
}
fn human_bytes(value: u64) -> String {
    let mut x = value as f64; let units = ["B","K","M","G","T"]; let mut i=0;
    while x >= 1024.0 && i < units.len()-1 { x/=1024.0; i+=1; }
    if i==0 { format!("{}{}", x as u64, units[i]) } else if x>=100.0 { format!("{x:.0}{}", units[i]) } else { format!("{x:.1}{}", units[i]) }
}
fn graph(hist: &VecDeque<f64>, width: usize, height: usize, max: f64, color: u8) -> Vec<String> {
    let raw: Vec<f64> = hist.iter().copied().collect(); let vals=smooth(&raw); let mut out=Vec::new();
    for y in (0..height).rev() {
        let mut line=String::new();
        for x in 0..width {
            let idx=if vals.len()<=width { x.saturating_sub(width-vals.len()) } else { x*vals.len()/width };
            let value=if x < width.saturating_sub(vals.len()) {0.0} else {vals.get(idx).copied().unwrap_or(0.0)};
            let level=(value/max.max(0.01)*height as f64).ceil() as usize;
            if level>y {
                let ratio=(value/max.max(0.01)).clamp(0.0,1.0);
                let shade=if ratio>=0.85 {203} else if ratio>=0.65 {220} else if ratio>=0.4 {45} else {color};
                line.push_str(&format!("\x1b[38;5;{shade}m•\x1b[0m"));
            } else { line.push(' '); }
        }
        out.push(line);
    }
    out
}

fn mini_graph(hist: &VecDeque<f64>, width: usize, color: u8) -> String {
    let raw: Vec<f64> = hist.iter().copied().collect(); let values=smooth(&raw);
    let peak = values.iter().copied().fold(1.0, f64::max);
    let mut out = String::new();
    for i in 0..width {
        let index = if values.len() <= width { i.saturating_sub(width - values.len()) }
            else { i * values.len() / width };
        let value = if i < width.saturating_sub(values.len()) { 0.0 } else { values.get(index).copied().unwrap_or(0.0) };
        let shade = if value / peak > 0.8 { color + 1 } else { color };
        out.push_str(&format!("\x1b[38;5;{shade}m•\x1b[0m"));
    }
    out
}

fn term_size() -> (usize, usize) {
    let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
    unsafe { libc::ioctl(1, libc::TIOCGWINSZ, &mut ws); }
    (ws.ws_col.max(50) as usize, ws.ws_row.max(18) as usize)
}
fn fit(s: &str, width: usize) -> String {
    let mut out=String::new(); let mut visible=0usize; let mut chars=s.chars().peekable();
    while let Some(c)=chars.next() {
        if c=='\x1b' {
            out.push(c);
            while let Some(n)=chars.next() {out.push(n); if n=='m' {break;}}
        } else if visible<width {out.push(c); visible+=1;}
    }
    if visible<width {out.push_str(&" ".repeat(width-visible));}
    if s.contains('\x1b') {out.push_str("\x1b[0m");}
    out
}
fn put(buf: &mut String, x: usize, y: usize, text: &str) { buf.push_str(&format!("\x1b[{};{}H{}", y+1,x+1,text)); }
fn border(buf: &mut String, x: usize, y: usize, w: usize, h: usize, title: &str) {
    if w<3 || h<2 { return; }
    put(buf,x,y,&format!("┌{}┐", "─".repeat(w-2)));
    put(buf,x,y,&format!("┌─ {} {}┐",title,"─".repeat(w.saturating_sub(title.len()+7))));
    for yy in 1..h-1 { put(buf,x,y+yy,"│"); put(buf,x+w-1,y+yy,"│"); }
    put(buf,x,y+h-1,&format!("└{}┘", "─".repeat(w-2)));
}

fn clear_panel(buf: &mut String, x: usize, y: usize, w: usize, h: usize) {
    if w < 3 || h < 3 { return; }
    let blank = " ".repeat(w - 2);
    for row in 1..h-1 { put(buf, x+1, y+row, &blank); }
}

fn render(d: &Data, agents: Option<&Value>, paused: bool) {
    let (cols, rows)=term_size(); let left=cols*34/100; let mid=cols*33/100; let right=cols-left-mid;
    let top=rows.saturating_sub(3); let cpu_h=top*43/100; let mem_h=top*20/100; let net_h=top*15/100; let disk_h=top-cpu_h-mem_h-net_h;
    let mut b=String::from("\x1b[?25l\x1b[38;5;45m");
    let host=d.host.as_str();
    let temp=if d.temp>0.0 {format!("{:.0}°C",d.temp)} else {"—".to_owned()};
    put(&mut b,0,0,&format!("\x1b[1;36mENVITOP-RS\x1b[0m  {host}  up {}h   {}  CPU {:.1}% {temp}  MEM {:.1}%  {}",d.uptime/3600,d.model,d.cpu,100.0*d.mem_used as f64/d.mem_total.max(1) as f64,if paused{"PAUSED"}else{"q quit · p pause"}));
    let cx=0; let cy=1; border(&mut b,cx,cy,left,cpu_h,"CPU"); clear_panel(&mut b,cx,cy,left,cpu_h);
    let label=fit(&format!("{}  {:.1}%  total {:.1}%  user {:.1}%  load {}",d.model,d.cpu,d.cpu,d.user_cpu,d.load),left.saturating_sub(4)); put(&mut b,cx+2,cy+1,&label);
    let core_cols=((left.saturating_sub(4))/13).max(1); let core_rows=(d.cores.len()+core_cols-1)/core_cols;
    for (i,p) in d.cores.iter().enumerate() { let col=i/core_rows.max(1); let row=i%core_rows.max(1); let x=cx+2+col*13; let y=cy+2+row; if y<cy+cpu_h.saturating_sub(5) { let t=d.core_temps.get(i).and_then(|v|*v).map(|v|format!(" {v:.0}°C")).unwrap_or_default(); put(&mut b,x,y,&format!("C{i:<2} {:>3.0}%{t}",p)); } }
    let graph_y=cy+2+core_rows.min(cpu_h.saturating_sub(7))+1; let graph_w=left.saturating_sub(4); let graph_h=cpu_h.saturating_sub(graph_y-cy)-2;
    for (i,line) in graph(&d.user_hist,graph_w,graph_h/2,100.0,42).iter().enumerate() { put(&mut b,cx+2,graph_y+i,line); }
    put(&mut b,cx+2,graph_y+graph_h/2,"USER CPU");
    for (i,line) in graph(&d.cpu_hist,graph_w,graph_h-graph_h/2,100.0,46).iter().enumerate() { put(&mut b,cx+2,graph_y+graph_h/2+1+i,line); }
    let my=cy+cpu_h; border(&mut b,0,my,left,mem_h,"MEMORY"); clear_panel(&mut b,0,my,left,mem_h);
    put(&mut b,2,my+1,&format!("{:.1}%  used {}/{}  available {}  cached {}",100.0*d.mem_used as f64/d.mem_total.max(1) as f64,human_bytes(d.mem_used),human_bytes(d.mem_total),human_bytes(d.mem_avail),human_bytes(d.mem_cached)));
    let ramh=(mem_h/2).saturating_sub(1).max(1); let swapy=my+2+ramh;
    for (i,line) in graph(&d.mem_hist,left.saturating_sub(4),ramh,100.0,42).iter().enumerate(){put(&mut b,2,my+2+i,line);}
    put(&mut b,2,swapy,&format!("SWAP USED  {} / {}",human_bytes(d.swap_used),human_bytes(d.swap_total)));
    for (i,line) in graph(&d.swap_hist,left.saturating_sub(4),1,100.0,135).iter().enumerate(){put(&mut b,2,swapy+1+i,line);}
    let ny=my+mem_h; border(&mut b,0,ny,left,net_h,"NETWORK"); clear_panel(&mut b,0,ny,left,net_h); put(&mut b,2,ny+1,&fit(&format!("RX {:>9}/s   TX {:>9}/s",human_bytes(d.rx as u64),human_bytes(d.tx as u64)),left.saturating_sub(4)));
    let nh=net_h.saturating_sub(5).max(2); let rxh=nh/2; let txh=nh-rxh;
    put(&mut b,2,ny+2,"RX ↓");
    for (i,line) in graph(&d.rx_hist,left.saturating_sub(4),rxh,d.rx_hist.iter().copied().fold(1.0,f64::max),42).iter().enumerate(){put(&mut b,2,ny+3+i,line);}
    let tx_y=ny+3+rxh; put(&mut b,2,tx_y,"TX ↑");
    for (i,line) in graph(&d.tx_hist,left.saturating_sub(4),txh,d.tx_hist.iter().copied().fold(1.0,f64::max),45).iter().enumerate(){put(&mut b,2,tx_y+1+i,line);}
    let dy=ny+net_h; border(&mut b,0,dy,left,disk_h,"DISK I/O · PARTITIONS"); clear_panel(&mut b,0,dy,left,disk_h);
    put(&mut b,2,dy+1,&format!("R {:>7}/s  W {:>7}/s  {} partitions",human_bytes(d.read as u64),human_bytes(d.write as u64),d.mounts.len()));
    put(&mut b,2,dy+2,"PARTITION   USED/TOTAL   FREE       READ/s NOW/PEAK          WRITE/s NOW/PEAK");
    for (i,m) in d.mounts.iter().take(disk_h.saturating_sub(4)).enumerate(){
        let read_peak=m.read_hist.iter().copied().fold(m.read, f64::max);
        let write_peak=m.write_hist.iter().copied().fold(m.write, f64::max);
        let rw=left.saturating_sub(4).saturating_sub(31)/2;
        let sw=rw.saturating_sub(12).min(14).max(4);
        let row=format!("{:<10} {:>7}/{:<7} {:>7}  {}{:>11}  {}{:>11}",
            fit(&m.path,10),human_bytes(m.used),human_bytes(m.total),human_bytes(m.free),
            mini_graph(&m.read_hist,sw,220),format!("{}/{}",human_bytes(m.read as u64),human_bytes(read_peak as u64)),
            mini_graph(&m.write_hist,sw,177),format!("{}/{}",human_bytes(m.write as u64),human_bytes(write_peak as u64)));
        put(&mut b,2,dy+3+i,&fit(&row,left.saturating_sub(4)));
    }
    let mx=left; border(&mut b,mx,1,mid,cpu_h,"GPU"); clear_panel(&mut b,mx,1,mid,cpu_h);
    if d.gpu.name.is_empty(){put(&mut b,mx+2,3,"GPU data waiting (nvidia-smi)");} else {
        put(&mut b,mx+2,3,&format!("{}  {:.0}%  {:.0}°C  {:.0}W",d.gpu.name,d.gpu.util,d.gpu.temp,d.gpu.power));
        put(&mut b,mx+2,4,&format!("VRAM {} / {}",human_bytes(d.gpu.used),human_bytes(d.gpu.total)));
        let gh=cpu_h.saturating_sub(8).max(2); let half=gh/2;
        put(&mut b,mx+2,5,"GPU UTIL");
        for (i,line) in graph(&d.gpu_hist,mid.saturating_sub(4),half,100.0,42).iter().enumerate(){put(&mut b,mx+2,6+i,line);}
        let mh_y=6+half; put(&mut b,mx+2,mh_y,"VRAM USED");
        for (i,line) in graph(&d.gpu_mem_hist,mid.saturating_sub(4),gh-half,100.0,45).iter().enumerate(){put(&mut b,mx+2,mh_y+1+i,line);}
    }
    let gpuy=1+cpu_h; border(&mut b,mx,gpuy,mid,mem_h,"GPU PROCESSES");
    put(&mut b,mx+2,gpuy+1,&fit("PID      G  SM% MEM%    VRAM  PROCESS",mid.saturating_sub(4)));
    let capacity=mem_h.saturating_sub(3);
    let visible=d.gpu.procs.len().min(capacity);
    let previous=LAST_GPU_PROCESS_ROWS.swap(visible,Ordering::Relaxed).min(capacity);
    for i in 0..visible.max(previous) {
        let row=d.gpu.procs.get(i).map(String::as_str).unwrap_or("");
        put(&mut b,mx+2,gpuy+2+i,&fit(row,mid.saturating_sub(4)));
    }
    let py=1+cpu_h+mem_h; border(&mut b,mx,py,mid,rows.saturating_sub(py+1),"PROCESSES"); clear_panel(&mut b,mx,py,mid,rows.saturating_sub(py+1));
    for (i,p) in d.procs.iter().take(rows.saturating_sub(py+3)).enumerate(){put(&mut b,mx+2,py+1+i,&fit(&format!("{:>7} {:>6} {:>6.1} {:>7}K {}",p.pid,p.user,p.cpu,p.rss_kb,p.cmd),mid.saturating_sub(4)));}
    let rx=left+mid; border(&mut b,rx,1,right,top*42/100,"AI AGENTS"); clear_panel(&mut b,rx,1,right,top*42/100);
    if let Some(a)=agents { if let Some(sessions)=a.get("sessions").and_then(Value::as_array) { for (i,s) in sessions.iter().take(10).enumerate(){
        let text=format!("{} {} {} {:.0}% {}",s["agent"].as_str().unwrap_or("AG"),s["project"].as_str().unwrap_or(""),s["model"].as_str().unwrap_or(""),s["context_pct"].as_f64().unwrap_or(0.0),human_bytes(s["total_tokens"].as_u64().unwrap_or(0)));
        put(&mut b,rx+2,3+i,&fit(&text,right.saturating_sub(4)));
    }} }
    let token_y=1+top*42/100; let token_h=top*25/100; border(&mut b,rx,token_y,right,token_h,"TOKENS"); clear_panel(&mut b,rx,token_y,right,token_h);
    if let Some(a)=agents {
        let s=a.get("sessions").and_then(Value::as_array).and_then(|v|v.first());
        put(&mut b,rx+2,token_y+1,&format!("Total  {}   rate {:.1}/min",human_bytes(a["total_tokens"].as_u64().unwrap_or(0)),a["token_rate"].as_f64().unwrap_or(0.0)*60.0));
        if let Some(session)=s {
            for (i,(key,label)) in [("input_tokens","Input"),("output_tokens","Output"),("cache_read","Cache read"),("cache_write","Cache write")].iter().enumerate() {
                put(&mut b,rx+2,token_y+2+i,&format!("{label:<11} {:>9}",human_bytes(session[*key].as_u64().unwrap_or(0))));
            }
        }
    }
    let detail_y=token_y+token_h; border(&mut b,rx,detail_y,right,top.saturating_sub(detail_y-1),"SESSION / QUOTAS"); clear_panel(&mut b,rx,detail_y,right,top.saturating_sub(detail_y-1));
    if let Some(a)=agents {
        let detail_h=top.saturating_sub(detail_y-1);
        if let Some(session)=a.get("sessions").and_then(Value::as_array).and_then(|v|v.first()) {
            put(&mut b,rx+2,detail_y+1,&fit(&format!("{}  {}",session["agent"].as_str().unwrap_or("Agent"),session["project"].as_str().unwrap_or("")),right.saturating_sub(4)));
            put(&mut b,rx+2,detail_y+2,&fit(&format!("model {}   context {:.0}%",session["model"].as_str().unwrap_or("—"),session["context_pct"].as_f64().unwrap_or(0.0)),right.saturating_sub(4)));
            if let Some(task)=session["current_task"].as_str(){put(&mut b,rx+2,detail_y+3,&fit(task,right.saturating_sub(4)));}
        }
        if let Some(quotas)=a.get("quotas").and_then(Value::as_array) {
            for (i,q) in quotas.iter().take(detail_h.saturating_sub(5)).enumerate(){
                put(&mut b,rx+2,detail_y+4+i,&fit(&format!("{}  5h {:>3.0}%   7d {:>3.0}%",q["source"].as_str().unwrap_or("quota"),q["five_pct"].as_f64().unwrap_or(0.0),q["seven_pct"].as_f64().unwrap_or(0.0)),right.saturating_sub(4)));
            }
        }
    }
    put(&mut b,0,rows-1,&format!("\x1b[0m  q quit    p pause     native Rust sampler · agent history from existing Python collector"));
    b.push_str("\x1b[0m\x1b[?25h"); let _=io::stdout().write_all(b.as_bytes()); let _=io::stdout().flush();
}

fn agent_sidecar() -> Receiver<Option<Value>> {
    let (tx,rx)=mpsc::channel();
    thread::spawn(move || loop {
        let script = r#"
import dataclasses, json, time, psutil
from envitop.collectors.agents import AgentCollector
from envitop.model import ProcInfo
c=AgentCollector(); p={}; prev=None; prev_at=time.monotonic()
while True:
  p={}
  for q in psutil.process_iter(['pid','name','cmdline','username','memory_info']):
    try:
      x=q.info; p[x['pid']]=ProcInfo(pid=x['pid'],name=x.get('name') or '',cmd=' '.join(x.get('cmdline') or []),user=x.get('username') or '',rss=x.get('memory_info').rss if x.get('memory_info') else 0)
    except Exception: pass
  s,r=c.collect(p,'recent',''); total=sum(x.active_tokens for x in s); now=time.monotonic()
  rate=0.0 if prev is None or total<prev else (total-prev)/max(0.001,now-prev_at)
  prev=total; prev_at=now
  print(json.dumps({'sessions':[dataclasses.asdict(x) for x in s],'quotas':[dataclasses.asdict(x) for x in r],'total_tokens':total,'token_rate':rate}),flush=True)
  time.sleep(2.5)
"#;
        let spawn=Command::new("python3").arg("-u").arg("-c").arg(script).current_dir(env!("CARGO_MANIFEST_DIR").to_string()+"/..").stdout(Stdio::piped()).stderr(Stdio::null()).spawn();
        if let Ok(mut child)=spawn {
            if let Some(out)=child.stdout.take() {
                let mut reader=io::BufReader::new(out); loop { let mut line=String::new(); if reader.read_line(&mut line).unwrap_or(0)==0 {break;} let value=serde_json::from_str(&line).ok(); if tx.send(value).is_err(){let _=child.kill();return;} }
            }
        }
        thread::sleep(Duration::from_secs(2));
    }); rx
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|arg| arg == "--version") {
        println!("envitop-native 0.1.0 (Rust)");
        return;
    }
    if args.iter().any(|arg| arg == "--help" || arg == "-h") {
        println!("envitop-native 0.1.0\nNative Linux resource monitor\n\nUsage: envitop-native [--help] [--version]\n\nKeys: q quit, p pause");
        return;
    }
    if !args.is_empty() {
        eprintln!("unsupported native options: {}. Use ENVITOP_PYTHON=1 for Python CLI options.", args.join(" "));
        std::process::exit(2);
    }
    let (mut old_cpu,mut old_cores)=read_cpu();
    let (mut old_rx,mut old_tx)=read_network(); let (mut old_read,mut old_write)=read_disks();
    let mut old_procs=HashMap::new(); let mut old_devices=read_disk_devices(); let mut data=Data::default(); data.model=cpu_model(); data.host=fs::read_to_string("/etc/hostname").unwrap_or_default().trim().to_owned();
    let mut raw:termios=unsafe{std::mem::zeroed()};
    if unsafe {tcgetattr(0,&mut raw)} != 0 { eprintln!("envitop-native requires an interactive terminal"); return; }
    let gpu_rx=gpu_thread(); let agent_rx=agent_sidecar(); let mut agent:Option<Value>=None; let mut paused=false;
    let mut cooked=raw; cooked.c_lflag &= !(ECHO|ICANON|ISIG); cooked.c_cc[VMIN]=0; cooked.c_cc[VTIME]=0; unsafe{tcsetattr(0,TCSANOW,&cooked);}
    print!("\x1b[?1049h\x1b[2J\x1b[?25l"); let _=io::stdout().flush(); let mut last=Instant::now();
    loop {
        let elapsed=last.elapsed().as_secs_f64().max(0.001); last=Instant::now();
        let (cpu_now,cores_now)=read_cpu(); let (cpu,user)=cpu_delta(&old_cpu,&cpu_now); data.cpu=cpu; data.user_cpu=user;
        data.cores=cores_now.iter().enumerate().map(|(i,c)| {let p=old_cores.get(i).cloned().unwrap_or_default(); cpu_delta(&p,c).0}).collect(); old_cpu=cpu_now; old_cores=cores_now;
        read_mem(&mut data); (data.temp,data.core_temps)=cpu_temps(data.cores.len());
        let (rx,tx)=read_network(); data.rx=rates(old_rx,rx,elapsed); data.tx=rates(old_tx,tx,elapsed); old_rx=rx;old_tx=tx;
        let (rd,wr)=read_disks();data.read=rates(old_read,rd,elapsed);data.write=rates(old_write,wr,elapsed);old_read=rd;old_write=wr;
        let mut mounts=read_mounts(&old_devices,elapsed); old_devices=read_disk_devices();
        for mount in &mut mounts {
            if let Some(old)=data.mounts.iter().find(|p|p.path==mount.path){ mount.read_hist=old.read_hist.clone(); mount.write_hist=old.write_hist.clone(); }
            push(&mut mount.read_hist,mount.read); push(&mut mount.write_hist,mount.write);
        }
        data.mounts=mounts;
        let (procs,next)=process_snapshot(&old_procs,elapsed);data.procs=procs;old_procs=next;
        data.load=fs::read_to_string("/proc/loadavg").unwrap_or_default().split_whitespace().take(3).collect::<Vec<_>>().join(" ");
        data.uptime=fs::read_to_string("/proc/uptime").unwrap_or_default().split_whitespace().next().unwrap_or("0").parse::<f64>().unwrap_or(0.0) as u64;
        push(&mut data.cpu_hist,data.cpu);push(&mut data.user_hist,data.user_cpu);push(&mut data.mem_hist,100.0*data.mem_used as f64/data.mem_total.max(1) as f64);push(&mut data.swap_hist,100.0*data.swap_used as f64/data.swap_total.max(1) as f64);
        push(&mut data.rx_hist,data.rx);push(&mut data.tx_hist,data.tx);push(&mut data.read_hist,data.read);push(&mut data.write_hist,data.write);
        if let Ok(g)=gpu_rx.try_recv(){data.gpu=g;if data.gpu.name.is_empty(){data.gpu_hist.clear();data.gpu_mem_hist.clear();}else{push(&mut data.gpu_hist,data.gpu.util);push(&mut data.gpu_mem_hist,100.0*data.gpu.used as f64/data.gpu.total.max(1) as f64);}} if let Ok(a)=agent_rx.try_recv(){agent=a;}
        if !paused {render(&data,agent.as_ref(),paused);}
        let mut pollfd=pollfd{fd:0,events:libc::POLLIN,revents:0};unsafe{poll(&mut pollfd,1,500);}
        if pollfd.revents & libc::POLLIN != 0 { let mut key=[0u8;8];if io::stdin().read(&mut key).unwrap_or(0)>0 {match key[0]{b'q'|3=>break,b'p'=>paused=!paused,_=>{}}} }
    }
    unsafe{tcsetattr(0,TCSANOW,&raw);} print!("\x1b[0m\x1b[?25h\x1b[?1049l");let _=io::stdout().flush();
}
