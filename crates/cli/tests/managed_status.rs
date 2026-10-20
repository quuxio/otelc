//! Compare real parent-observed termination through every managed launch branch.
#![cfg(unix)]
#[path = "managed_status/receiver.rs"]
mod receiver;
use std::{
    fs,
    io::{BufRead, BufReader, Read},
    os::unix::process::ExitStatusExt,
    path::{Path, PathBuf},
    process::{Child, Command, Output, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn checked(command: &mut Command) {
    let output = command.output().unwrap();
    assert!(
        output.status.success(),
        "{command:?}: {}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn prepare(language: &str, directory: &Path) -> (Vec<String>, Vec<String>) {
    match language {
        "python" => {
            let local = repo().join(".venv/bin/python");
            let python = std::env::var("OTELC_PYTHON").unwrap_or_else(|_| {
                if local.is_file() {
                    local.display().to_string()
                } else {
                    "python3".into()
                }
            });
            fs::write(directory.join("termination.py"), "import os,sys,time\ncode=int(sys.argv[1])\nif code>=0: raise SystemExit(code)\nwith open('ready.pid','w') as file: file.write(str(os.getpid()))\nprint('ready',flush=True)\ntime.sleep(60)\n").unwrap();
            (
                vec![python, "termination.py".into()],
                vec!["python".into(), "termination.py".into()],
            )
        }
        "javascript" | "typescript" => {
            let filename = if language == "typescript" {
                "termination.mts"
            } else {
                "termination.mjs"
            };
            let annotation = if language == "typescript" {
                ": number"
            } else {
                ""
            };
            fs::write(directory.join(filename), format!("import {{writeFileSync}} from 'node:fs';\nconst code{annotation}=Number(process.argv[2]);\nif(code>=0) process.exit(code);\nwriteFileSync('ready.pid',String(process.pid));\nconsole.log('ready');\nsetTimeout(()=>{{}},60000);\n")).unwrap();
            let (plain, adapter) = if language == "typescript" {
                checked(
                    Command::new("node")
                        .arg(repo().join("adapters/node/node_modules/typescript-native/bin/tsc"))
                        .current_dir(directory)
                        .args([
                            filename,
                            "--noCheck",
                            "--module",
                            "nodenext",
                            "--outDir",
                            "plain",
                        ]),
                );
                ("plain/termination.mjs", "ts")
            } else {
                (filename, "node")
            };
            (
                vec!["node".into(), plain.into()],
                vec![adapter.into(), filename.into()],
            )
        }
        "java" => {
            fs::write(directory.join("Termination.java"), "import java.nio.file.*; public class Termination {public static void main(String[] args) throws Exception {int code=Integer.parseInt(args[0]); if(code>=0)System.exit(code); Files.writeString(Path.of(\"ready.pid\"),Long.toString(ProcessHandle.current().pid()));System.out.println(\"ready\");Thread.sleep(60000);}}\n").unwrap();
            checked(
                Command::new("javac")
                    .current_dir(directory)
                    .arg("Termination.java"),
            );
            (
                vec![
                    "java".into(),
                    "-cp".into(),
                    ".".into(),
                    "Termination".into(),
                ],
                vec![
                    "java".into(),
                    "-cp".into(),
                    ".".into(),
                    "Termination".into(),
                ],
            )
        }
        "go" => {
            fs::write(directory.join("termination.go"), "package main\nimport(\"os\";\"strconv\";\"fmt\";\"time\")\nfunc main(){code,_:=strconv.Atoi(os.Args[1]);if code>=0{os.Exit(code)};os.WriteFile(\"ready.pid\",[]byte(strconv.Itoa(os.Getpid())),0600);fmt.Println(\"ready\");time.Sleep(60*time.Second)}\n").unwrap();
            checked(Command::new("go").current_dir(directory).args([
                "build",
                "-o",
                "plain",
                "termination.go",
            ]));
            (
                vec![directory.join("plain").display().to_string()],
                vec!["go".into(), "termination.go".into()],
            )
        }
        "rust" => {
            fs::write(directory.join("termination.rs"), "fn main(){let code:i32=std::env::args().nth(1).unwrap().parse().unwrap();if code>=0{std::process::exit(code)}std::fs::write(\"ready.pid\",std::process::id().to_string()).unwrap();println!(\"ready\");std::thread::sleep(std::time::Duration::from_secs(60));}\n").unwrap();
            checked(Command::new("rustc").current_dir(directory).args([
                "-o",
                "plain",
                "termination.rs",
            ]));
            (
                vec![directory.join("plain").display().to_string()],
                vec!["rust".into(), "termination.rs".into()],
            )
        }
        _ => unreachable!(),
    }
}

fn captured_output(child: Child, reader: Option<thread::JoinHandle<Vec<u8>>>) -> Output {
    let mut output = child.wait_with_output().unwrap();
    if let Some(reader) = reader {
        output.stdout = reader.join().unwrap();
    }
    output
}

fn launch(arguments: &[String], directory: &Path, code: i32, signal: Option<i32>) -> Output {
    let ready = directory.join("ready.pid");
    let _ = fs::remove_file(&ready);
    // Generated TypeScript copies must live outside the source project.
    let scratch = tempfile::tempdir().unwrap();
    let mut command = Command::new(&arguments[0]);
    for (name, _) in std::env::vars_os() {
        if name.to_string_lossy().starts_with("OTEL_") {
            command.env_remove(name);
        }
    }
    let mut child = command
        .args(&arguments[1..])
        .arg(code.to_string())
        .current_dir(directory)
        .env_remove("OTELC_LANGUAGE")
        .env_remove("OTELC_CONFIG")
        .env("TMPDIR", scratch.path())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let (ready_sender, ready_output) = mpsc::sync_channel(1);
    let output_reader = signal.map(|_| {
        let stdout = child.stdout.take().unwrap();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut bytes = Vec::new();
            reader.read_until(b'\n', &mut bytes).unwrap();
            let _ = ready_sender.send(bytes.clone());
            reader.read_to_end(&mut bytes).unwrap();
            bytes
        })
    });
    let mut deadline = Instant::now() + Duration::from_secs(120);
    if let Some(signal) = signal {
        let mut output_ready = false;
        let pid = loop {
            if let Ok(line) = ready_output.try_recv() {
                if line != b"ready\n" {
                    let _ = child.kill();
                    panic!(
                        "unexpected readiness output: {:?}",
                        captured_output(child, output_reader)
                    );
                }
                output_ready = true;
            }
            if let Ok(value) = fs::read_to_string(&ready) {
                if let Ok(pid) = value.parse::<u32>() {
                    // The PID file can precede stdout. In particular SIGKILL
                    // must not race the application-visible readiness write.
                    if output_ready {
                        break pid;
                    }
                }
            }
            if child.try_wait().unwrap().is_some() {
                panic!(
                    "target exited before readiness: {:?}",
                    captured_output(child, output_reader)
                );
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                panic!(
                    "target did not become ready: {:?}",
                    captured_output(child, output_reader)
                );
            }
            thread::sleep(Duration::from_millis(5));
        };
        checked(Command::new("kill").args([format!("-{signal}"), pid.to_string()]));
        deadline = Instant::now() + Duration::from_secs(5);
    }
    loop {
        if child.try_wait().unwrap().is_some() {
            let output = captured_output(child, output_reader);
            let leftovers: Vec<_> = fs::read_dir(scratch.path())
                .unwrap()
                .map(|entry| entry.unwrap().file_name())
                .filter(|name| {
                    let name = name.to_string_lossy();
                    name.starts_with(".tmp") || name.starts_with("otelc-")
                })
                .collect();
            assert!(
                leftovers.is_empty(),
                "launcher left temporary files after {arguments:?}: {leftovers:?}"
            );
            return output;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!(
                "launcher did not terminate: {:?}",
                captured_output(child, output_reader)
            );
        }
        thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn early_exit_keeps_complete_stdout_and_stderr_in_the_diagnostic() {
    let directory = tempfile::tempdir().unwrap();
    let (arguments, _) = prepare("python", directory.path());
    fs::write(directory.path().join("termination.py"), "import sys\nprint('ready',flush=True)\nprint('diagnostic-tail',flush=True)\nprint('diagnostic-stderr',file=sys.stderr,flush=True)\nraise SystemExit(7)\n").unwrap();
    let failure = std::panic::catch_unwind(|| launch(&arguments, directory.path(), -1, Some(15)))
        .expect_err("fixture should exit before publishing its PID");
    let message = failure.downcast_ref::<String>().unwrap();
    assert!(message.contains("diagnostic-tail"), "{message}");
    assert!(message.contains("diagnostic-stderr"), "{message}");
}

#[test]
fn signal_waits_for_ready_output_after_the_pid_is_published() {
    let directory = tempfile::tempdir().unwrap();
    let (arguments, _) = prepare("python", directory.path());
    fs::write(directory.path().join("termination.py"), "import os,time\nwith open('ready.pid','w') as file: file.write(str(os.getpid()))\ntime.sleep(0.25)\nprint('ready',flush=True)\ntime.sleep(60)\n").unwrap();
    for signal in [15, 9] {
        let output = launch(&arguments, directory.path(), -1, Some(signal));
        assert_eq!(output.status.signal(), Some(signal));
        assert_eq!(output.stdout, b"ready\n");
        assert!(output.stderr.is_empty());
    }
}

#[test]
fn all_managed_languages_preserve_parent_observed_termination() {
    let receiver = receiver::Receiver::new();
    let endpoint = &receiver.endpoint;
    for language in ["python", "javascript", "typescript", "java", "go", "rust"] {
        let root = tempfile::tempdir().unwrap();
        let directory = root.path();
        let backend = if language == "typescript" {
            "[adapters.typescript]\nbackend='native'\n"
        } else {
            ""
        };
        fs::write(directory.join("otelc.toml"), format!("schema_version=2\nlanguages=['{language}']\n[metrics]\nenabled=false\n[functions]\ninclude=['never_selected']\n[runtime]\nshutdown_timeout_ms=2000\n[export]\nendpoint='{endpoint}'\ntimeout_ms=1000\ninterval_ms=60000\n{backend}")).unwrap();
        let (plain, adapter) = prepare(language, directory);
        let sources: Vec<_> = fs::read_dir(directory)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| {
                matches!(
                    path.extension().and_then(|extension| extension.to_str()),
                    Some("py" | "mjs" | "mts" | "java" | "go" | "rs" | "toml")
                )
            })
            .map(|path| {
                let bytes = fs::read(&path).unwrap();
                (path, bytes)
            })
            .collect();
        let mut measured = vec![env!("CARGO_BIN_EXE_quux-otelc").into()];
        measured.extend(adapter);
        for (code, signal) in [(0, None), (7, None), (-1, Some(15)), (-1, Some(9))] {
            let baseline = launch(&plain, directory, code, signal);
            let actual = launch(&measured, directory, code, signal);
            let diagnostic = format!(
                "{language} code={code}, signal={signal:?}: {}",
                String::from_utf8_lossy(&actual.stderr)
            );
            assert_eq!(baseline.status.code(), actual.status.code(), "{diagnostic}");
            assert_eq!(
                baseline.status.signal(),
                actual.status.signal(),
                "{diagnostic}"
            );
            assert_eq!(baseline.stdout, actual.stdout, "{diagnostic}");
            assert_eq!(baseline.stderr, actual.stderr, "{diagnostic}");
        }
        for (source, original) in sources {
            assert_eq!(fs::read(source).unwrap(), original);
        }
    }
}
