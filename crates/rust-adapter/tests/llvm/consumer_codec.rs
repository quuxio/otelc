fn echo(value:u128)->u128{eprintln!("app-stderr:echo");value}
fn decimal(value:f32)->f32{value}
fn logical_source_length()->usize{file!().len()}
fn logical_source_column()->usize{column!() as usize}
fn intentional_error()->i32{std::panic::set_hook(Box::new(|_|eprintln!("app-panic:expected")));std::panic::panic_any("expected")}
fn unstable_error()->i32{eprintln!("case-pid:{}",std::process::id());std::panic::panic_any("expected")}

extern crate std as __oteleq_std;
#[allow(non_camel_case_types)]
trait __oteleq_Value { fn capture(&self)->__oteleq_std::string::String; }
macro_rules! __oteleq_scalar {($(($label:ident,$kind:ty)),*)=>{$(
  impl __oteleq_Value for $kind {
    fn capture(&self)->__oteleq_std::string::String {__oteleq_std::format!("{}:{:?}",__oteleq_std::stringify!($label),self)}
  }
)*}}
__oteleq_scalar!((bool,__oteleq_std::primitive::bool),(char,__oteleq_std::primitive::char),
  (i8,__oteleq_std::primitive::i8),(i16,__oteleq_std::primitive::i16),(i32,__oteleq_std::primitive::i32),
  (i64,__oteleq_std::primitive::i64),(i128,__oteleq_std::primitive::i128),(isize,__oteleq_std::primitive::isize),
  (u8,__oteleq_std::primitive::u8),(u16,__oteleq_std::primitive::u16),(u32,__oteleq_std::primitive::u32),
  (u64,__oteleq_std::primitive::u64),(u128,__oteleq_std::primitive::u128),(usize,__oteleq_std::primitive::usize),
  (String,__oteleq_std::string::String),(str,&__oteleq_std::primitive::str),(unit,()));
impl __oteleq_Value for __oteleq_std::primitive::f32 {
  fn capture(&self)->__oteleq_std::string::String {
    __oteleq_std::format!("f32:{:08x}",__oteleq_std::primitive::f32::to_bits(*self))
  }
}
impl __oteleq_Value for __oteleq_std::primitive::f64 {
  fn capture(&self)->__oteleq_std::string::String {
    __oteleq_std::format!("f64:{:016x}",__oteleq_std::primitive::f64::to_bits(*self))
  }
}
fn __oteleq_value<T:__oteleq_Value>(value:&T)->__oteleq_std::string::String {value.capture()}

fn main() {
    let mut s=__oteleq_std::format!("abi:{{\"pointer_bits\":{}}}\nbefore\n",__oteleq_std::mem::size_of::<__oteleq_std::primitive::usize>()*8); 
    match __oteleq_std::panic::catch_unwind(|| echo(340282366920938463463374607431768211455u128)) {
      Ok(v)=>s.push_str(&__oteleq_std::format!("return:{}\n",__oteleq_value(&v))),
      Err(payload)=>{
        if let Some(v)=payload.downcast_ref::<__oteleq_std::string::String>() { s.push_str(&__oteleq_std::format!("error:string:{:?}\n",v)); }
        else if let Some(v)=payload.downcast_ref::<&__oteleq_std::primitive::str>() { s.push_str(&__oteleq_std::format!("error:str:{:?}\n",v)); }
        else { __oteleq_std::process::exit(91); }
      },
    }
    s.push_str("after\n"); 
    __oteleq_std::fs::write(__oteleq_std::env::var("OTELEQ_OBSERVATION").unwrap(),s).unwrap();
}
