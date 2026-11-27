//! Orchestrated over a local socket; all reported operation timings are internal.
use lean_vm::lean_xmss::{LeanXmssPublicKey, LeanXmssSignature, SIG_SIZE};
use lean_vm::{AggregateClaims, AggregateProof, OutputClaims};
use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::time::Instant;

#[global_allocator]
static ALLOC: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;

const LIMIT: usize = 256 * 1024 * 1024;
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

fn describe() -> String {
    format!(
        "{{\"backend\":\"leanvm-in-process\",\"protocol_version\":{},\"cpu_tier\":\"{:?}\",\"available_tiers\":{:?}}}",
        lean_vm::PROTOCOL_VERSION,
        experimental_dispatch::tier(),
        experimental_dispatch::__tiers_here()
            .iter()
            .map(|t| format!("{t:?}"))
            .collect::<Vec<_>>()
    )
}

struct Reader<'a>(&'a [u8]);
impl<'a> Reader<'a> {
    fn take(&mut self, count: usize) -> Result<&'a [u8]> {
        if count > self.0.len() {
            return Err("truncated request".into());
        }
        let (value, rest) = self.0.split_at(count);
        self.0 = rest;
        Ok(value)
    }
    fn byte(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }
    fn count(&mut self) -> Result<usize> {
        let value = usize::try_from(u64::from_le_bytes(self.take(8)?.try_into()?))?;
        if value > LIMIT {
            return Err("oversized field".into());
        }
        Ok(value)
    }
    fn field(&mut self) -> Result<&'a [u8]> {
        let n = self.count()?;
        self.take(n)
    }
    fn finish(&self) -> Result<()> {
        if self.0.is_empty() {
            Ok(())
        } else {
            Err("trailing request bytes".into())
        }
    }
}
fn field(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend((bytes.len() as u64).to_le_bytes());
    out.extend(bytes);
}
fn write(stream: &mut UnixStream, body: &[u8]) -> Result<()> {
    stream.write_all(&(body.len() as u64).to_le_bytes())?;
    stream.write_all(body)?;
    Ok(())
}
fn claims(bytes: &[u8]) -> Result<AggregateClaims> {
    AggregateClaims::from_bytes(bytes).ok_or_else(|| "invalid claims".into())
}
fn aggregate(reader: &mut Reader<'_>) -> Result<(Vec<u8>, f64)> {
    let rate = usize::from(reader.byte()?);
    let count = reader.count()?;
    if count > lean_vm::MAX_RECURSIONS {
        return Err("too many children".into());
    }
    let mut children = Vec::new();
    for _ in 0..count {
        children.push((claims(reader.field()?)?, reader.field()?.to_vec()));
    }
    let count = reader.count()?;
    let mut signatures = Vec::new();
    for _ in 0..count {
        signatures.push((
            LeanXmssPublicKey::from_bytes(reader.take(32)?.try_into()?),
            reader.take(32)?.try_into()?,
            LeanXmssSignature::from_bytes(reader.take(SIG_SIZE)?.try_into()?),
        ));
    }
    if reader.count()? != 0 || reader.count()? != 0 {
        return Err("only XMSS workloads supported".into());
    }
    let output = match reader.byte()? {
        0 => None,
        1 => Some(claims(reader.field()?)?),
        _ => return Err("invalid output selector".into()),
    };
    reader.finish()?;
    let expected = if let Some(output) = &output {
        output.clone()
    } else {
        let mut all: Vec<_> = children
            .iter()
            .flat_map(|(claims, _)| claims.lean_xmss.iter().copied())
            .chain(signatures.iter().map(|(key, message, _)| (*key, *message)))
            .collect();
        all.sort_unstable();
        all.dedup();
        AggregateClaims {
            lean_xmss: all,
            lean_sphincs: vec![],
            da_commitment: None,
        }
    };
    // Match Lighthouse's InProcessBackend::aggregate: deserialize child proofs,
    // invoke the library, check output claims, serialize the proof. Socket and
    // request decoding, fixture work, and initialization are outside this timer.
    let started = Instant::now();
    let children = children
        .into_iter()
        .map(|(claims, bytes)| {
            AggregateProof::from_bytes(&bytes)
                .map(|proof| (claims, proof))
                .ok_or("invalid child")
        })
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let (result_claims, proof) = lean_vm::aggregate(
        &children,
        signatures,
        vec![],
        None,
        output
            .as_ref()
            .map_or(OutputClaims::All, OutputClaims::Only),
        rate,
    )?;
    if result_claims != expected {
        return Err("unexpected output claims".into());
    }
    let proof = proof.to_bytes();
    let elapsed = started.elapsed().as_secs_f64() * 1000.0;
    let mut result = Vec::new();
    field(&mut result, &result_claims.to_bytes());
    field(&mut result, &proof);
    Ok((result, elapsed))
}
fn verify(reader: &mut Reader<'_>) -> Result<(Vec<u8>, f64)> {
    let claims = claims(reader.field()?)?;
    let bytes = reader.field()?;
    reader.finish()?;
    let started = Instant::now();
    let valid = AggregateProof::from_bytes(bytes).is_some_and(|proof| proof.verify(&claims));
    Ok((
        vec![u8::from(valid)],
        started.elapsed().as_secs_f64() * 1000.0,
    ))
}
fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() == 2 && args[1] == "--describe" {
        println!("{}", describe());
        return Ok(());
    }
    if args.len() != 5 || args[1] != "--socket" || args[3] != "--threads" {
        return Err("usage: pq-bench-worker --describe | --socket PATH --threads N".into());
    }
    lean_vm::setup(args[4].parse()?);
    let listener = UnixListener::bind(&args[2])?;
    let (mut stream, _) = listener.accept()?;
    let mut hello = b"leanvmsc\x02\x00\x00\x00".to_vec();
    hello.extend((LIMIT as u64).to_le_bytes());
    field(&mut hello, b"leanvm-v1");
    field(&mut hello, b"experimental");
    field(&mut hello, describe().as_bytes());
    write(&mut stream, &hello)?;
    loop {
        let mut length = [0; 8];
        if let Err(error) = stream.read_exact(&mut length) {
            if error.kind() == std::io::ErrorKind::UnexpectedEof {
                return Ok(());
            }
            return Err(error.into());
        }
        let length = usize::try_from(u64::from_le_bytes(length))?;
        if length > LIMIT {
            return Err("oversized request".into());
        }
        let mut bytes = vec![0; length];
        stream.read_exact(&mut bytes)?;
        let mut reader = Reader(&bytes);
        let result = match reader.byte()? {
            1 => aggregate(&mut reader),
            2 => verify(&mut reader),
            _ => Err("invalid operation".into()),
        };
        match result {
            Ok((payload, elapsed)) => {
                let mut response = vec![0];
                response.extend(payload);
                response.extend(elapsed.to_le_bytes());
                write(&mut stream, &response)?;
            }
            Err(error) => {
                let mut response = vec![1];
                response.extend(error.to_string().as_bytes());
                write(&mut stream, &response)?;
                return Err(error);
            }
        }
    }
}
