//! Optional coverage evidence survives the ordinary fixture's temporary workspace.
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
};

pub struct Capture {
    root: PathBuf,
}
impl Capture {
    pub fn new(
        temporary: &Path,
        sources: &BTreeMap<PathBuf, String>,
        plan: &Path,
        request: &Path,
    ) -> Option<Self> {
        let destination = std::env::var_os("OTELC_RUST_MIR_COVERAGE")?;
        let root = tempfile::Builder::new()
            .prefix("pair-")
            .tempdir_in(destination)
            .unwrap()
            .keep();
        let temporary = temporary.canonicalize().unwrap();
        for source in sources.keys() {
            let target = root
                .join("source")
                .join(source.strip_prefix(&temporary).unwrap());
            fs::create_dir_all(target.parent().unwrap()).unwrap();
            fs::copy(source, target).unwrap();
        }
        fs::copy(plan, root.join("plan.json")).unwrap();
        fs::copy(request, root.join("request.json")).unwrap();
        Some(Self { root })
    }
    fn lane(&self, measured: bool) -> PathBuf {
        self.root
            .join(if measured { "instrumented" } else { "plain" })
    }
    pub fn configure(&self, command: &mut Command, measured: bool) {
        let directory = self.lane(measured).join("profiles");
        fs::create_dir_all(&directory).unwrap();
        command.env("LLVM_PROFILE_FILE", directory.join("%p-%m.profraw"));
    }
    pub fn record(&self, measured: bool, binary: &Path, output: &Output) {
        let lane = self.lane(measured);
        fs::copy(binary, lane.join("application")).unwrap();
        fs::write(lane.join("stdout"), &output.stdout).unwrap();
        fs::write(lane.join("stderr"), &output.stderr).unwrap();
        fs::write(
            lane.join("status.json"),
            serde_json::to_vec(&output.status.code()).unwrap(),
        )
        .unwrap();
    }
    pub fn complete(self, report: &Value, expected: u64) {
        fs::write(
            self.root.join("runtime.json"),
            serde_json::to_vec_pretty(report).unwrap(),
        )
        .unwrap();
        let lane = self.lane(true);
        let manifest = json!({"schema_version": 1, "object": "application",
            "object_sha256": super::sha(&lane.join("application")),
            "profiles": "profiles", "expected_functions_and_spans": expected,
            "source_hashes": serde_json::from_slice::<Value>(&fs::read(self.root.join("request.json")).unwrap()).unwrap()["sources"]});
        fs::write(
            lane.join("coverage.json"),
            serde_json::to_vec_pretty(&manifest).unwrap(),
        )
        .unwrap();
    }
}
