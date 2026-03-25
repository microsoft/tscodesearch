// query_fixture.rs — synthetic Rust file for query_rust test suite.
// Contains no project-specific references; safe for public use.

use std::collections::HashMap;
use std::fmt;

// -- Traits -------------------------------------------------------------------

pub trait Processor {
    fn process(&self, input: &str) -> String;
    fn reset(&mut self);
}

pub trait Logger {
    fn log(&self, message: &str);
    fn warn(&self, message: &str);
}

// -- Structs ------------------------------------------------------------------

pub struct ProcessResult {
    pub success: bool,
    pub output: String,
    pub error_code: i32,
}

impl ProcessResult {
    pub fn new(success: bool, output: String, error_code: i32) -> Self {
        ProcessResult { success, output, error_code }
    }

    pub fn ok(output: String) -> Self {
        ProcessResult { success: true, output, error_code: 0 }
    }
}

// -- Enums --------------------------------------------------------------------

pub enum ProcessingMode {
    Sequential,
    Parallel,
    Batch,
}

// -- Impl Trait for Struct ----------------------------------------------------

pub struct TextProcessor {
    prefix: String,
    logger: Box<dyn Logger>,
}

impl TextProcessor {
    pub fn new(prefix: &str, logger: Box<dyn Logger>) -> Self {
        TextProcessor {
            prefix: prefix.to_string(),
            logger,
        }
    }

    pub fn format(&self, input: &str) -> String {
        format!("{}{}", self.prefix, input)
    }
}

impl Processor for TextProcessor {
    fn process(&self, input: &str) -> String {
        // COMMENT: process() is mentioned here but not a real call
        let result = self.format(input);
        self.logger.log(&result);
        result
    }

    fn reset(&mut self) {
        self.prefix.clear();
    }
}

// -- Factory function ---------------------------------------------------------

pub fn create_processor(prefix: &str, logger: Box<dyn Logger>) -> TextProcessor {
    TextProcessor::new(prefix, logger)
}

pub fn run_processor(processor: &dyn Processor, input: &str) -> ProcessResult {
    let output = processor.process(input);
    ProcessResult::ok(output)
}

// -- Service struct -----------------------------------------------------------

pub struct ProcessingService {
    processor: Box<dyn Processor>,
    cache: HashMap<String, ProcessResult>,
}

impl ProcessingService {
    pub fn new(processor: Box<dyn Processor>) -> Self {
        ProcessingService {
            processor,
            cache: HashMap::new(),
        }
    }

    pub fn do_work(&mut self, input: &str) -> ProcessResult {
        let result = run_processor(self.processor.as_ref(), input);
        result
    }

    pub fn inspect_result(&self, result: &ProcessResult) -> bool {
        // member accesses on ProcessResult
        if result.success {
            println!("{}", result.output);
        }
        result.success
    }
}

// -- Corner cases -------------------------------------------------------------

pub fn corner_cases() {
    // Call in string literal should NOT be matched: "process()"
    let _s = "process() is just a string";

    // Nested call
    let text = TextProcessor::new("hello", Box::new(DummyLogger));
    let _ = text.process("world");
    let _ = create_processor("test", Box::new(DummyLogger));
}

struct DummyLogger;

impl Logger for DummyLogger {
    fn log(&self, message: &str) {
        println!("{}", message);
    }
    fn warn(&self, message: &str) {
        eprintln!("{}", message);
    }
}

impl fmt::Display for ProcessResult {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "ProcessResult({})", self.success)
    }
}
