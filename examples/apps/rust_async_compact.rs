#![deny(warnings)]
use std::{future::Future, task::{Context, Poll, Waker}};
fn drive<F:Future>(future:F)->F::Output {let mut task=Box::pin(future);match task.as_mut().poll(&mut Context::from_waker(Waker::noop())) {Poll::Ready(value)=>value,Poll::Pending=>panic!("unexpected pending")}}
fn double(value:i32)->i32 {value*2}
async fn number()->i32{42}
async fn block()->i32{{let value=42;value}}
async fn borrowed()->&'static str{"original"}
async fn pointer()->fn(i32)->i32{double}
async fn boxed()->Box<dyn std::fmt::Display>{Box::new(7)}
async fn empty(){}
async fn early()->i32{return 42}
fn main() {assert_eq!(drive(number()),42);assert_eq!(drive(block()),42);assert_eq!(drive(borrowed()),"original");assert_eq!(drive(pointer())(3),6);assert_eq!(drive(boxed()).to_string(),"7");drive(empty());assert_eq!(drive(early()),42);println!("compact returns preserved");}
