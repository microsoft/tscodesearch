// query_fixture.ts — synthetic TypeScript file for query_js test suite.
// Contains no project-specific references; safe for public use.

// -- Imports ------------------------------------------------------------------

import { EventEmitter } from 'events';

// -- Interfaces ---------------------------------------------------------------

interface IProcessor {
    process(input: string): string;
    reset(): void;
}

interface ILogger {
    log(message: string): void;
    warn(message: string): void;
}

// -- Type aliases -------------------------------------------------------------

type ProcessResult = {
    success: boolean;
    output: string;
    errorCode: number;
};

// -- Enum ---------------------------------------------------------------------

enum ProcessingMode {
    Sequential = 'sequential',
    Parallel = 'parallel',
    Batch = 'batch',
}

// -- Decorators ---------------------------------------------------------------

function serializable(constructor: Function) {
    return constructor;
}

function deprecated(target: any, key: string, descriptor: PropertyDescriptor) {
    return descriptor;
}

// -- Abstract base class ------------------------------------------------------

abstract class BaseProcessor implements IProcessor {
    protected logger: ILogger;

    constructor(logger: ILogger) {
        this.logger = logger;
    }

    abstract process(input: string): string;

    reset(): void {
        // default: no-op
    }
}

// -- Concrete class -----------------------------------------------------------

@serializable
class TextProcessor extends BaseProcessor implements IProcessor {
    private prefix: string;

    constructor(prefix: string, logger: ILogger) {
        super(logger);
        this.prefix = prefix;
    }

    format(input: string): string {
        return this.prefix + input;
    }

    process(input: string): string {
        // COMMENT: process() is mentioned here but not a real call
        const result = this.format(input);
        this.logger.log(result);
        return result;
    }

    @deprecated
    legacyProcess(input: string): string {
        return this.process(input);
    }
}

// -- Factory ------------------------------------------------------------------

function createProcessor(prefix: string, logger: ILogger): TextProcessor {
    return new TextProcessor(prefix, logger);
}

function runProcessor(processor: IProcessor, input: string): ProcessResult {
    const output = processor.process(input);
    return { success: true, output, errorCode: 0 };
}

// -- Service class ------------------------------------------------------------

class ProcessingService extends EventEmitter {
    private processor: IProcessor;

    constructor(processor: IProcessor) {
        super();
        this.processor = processor;
    }

    doWork(input: string): ProcessResult {
        const output = runProcessor(this.processor, input);
        return output;
    }

    getProcessor(): IProcessor {
        return this.processor;
    }
}

// -- Corner cases -------------------------------------------------------------

function cornerCases(): void {
    // Call in string should NOT match: "process()"
    const s = "process() is just a string";

    const logger: ILogger = { log: console.log, warn: console.warn };
    const proc = createProcessor("hello", logger);
    proc.process("world");
    runProcessor(proc, "test");
}

export { IProcessor, ILogger, BaseProcessor, TextProcessor, ProcessingService,
         ProcessingMode, createProcessor, runProcessor };
