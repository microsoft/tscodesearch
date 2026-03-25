// query_fixture.js — synthetic JavaScript file for query_js test suite.
// Contains no project-specific references; safe for public use.

// -- Imports ------------------------------------------------------------------

import { EventEmitter } from 'events';

// -- Classes ------------------------------------------------------------------

class Processor {
    constructor(options) {
        this.options = options;
    }

    process(input) {
        // COMMENT: process() is mentioned here but not a real call
        return input;
    }

    reset() {
        this.options = {};
    }
}

class Logger {
    log(message) {
        console.log(message);
    }

    warn(message) {
        console.warn(message);
    }
}

class TextProcessor extends Processor {
    constructor(prefix, logger) {
        super({ prefix });
        this.prefix = prefix;
        this.logger = logger;
    }

    format(input) {
        return this.prefix + input;
    }

    process(input) {
        const result = this.format(input);
        this.logger.log(result);
        return result;
    }
}

// -- Factory ------------------------------------------------------------------

function createProcessor(prefix, logger) {
    return new TextProcessor(prefix, logger);
}

function runProcessor(processor, input) {
    return processor.process(input);
}

// -- Service class ------------------------------------------------------------

class ProcessingService extends EventEmitter {
    constructor(processor) {
        super();
        this.processor = processor;
        this.results = [];
    }

    doWork(input) {
        const output = runProcessor(this.processor, input);
        this.results.push(output);
        return output;
    }

    getProcessor() {
        return this.processor;
    }
}

// -- Corner cases -------------------------------------------------------------

function cornerCases() {
    // Call in string should NOT match: "process()"
    const s = "process() is just a string";

    const logger = new Logger();
    const proc = createProcessor("hello", logger);
    proc.process("world");
    runProcessor(proc, "test");
}

export { Processor, Logger, TextProcessor, ProcessingService, createProcessor, runProcessor };
