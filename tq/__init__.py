#!/usr/bin/python3
# TQ (Task Queue), simple Command Line Job Manager, together with client utilities
# COPYRIGHT (C) 2016-2018 Mo Zhou <cdluminate@gmail.com>
# MIT License

# ? FIXME: unify the tq*() interfaces and setup a argv->func mapping. this simplifies main function
# ? FIXME: tidy up the logging
# ? FIXME: tidy up the code
# ? FIXME: put the ANSI excape code wrappers into a single Class or function
# ? FIXME: replace the current argument parsing with argparse or alike. However the argument definition of TQ is somewhat abnormal ...
# ? FIXME: tq autostart? I guess no.
# ? FIXME: batch dbQuery

from pprint import pprint
from typing import *
import atexit
import io
import logging as log
import math
import multiprocessing as mp
import os
import re
import select
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import time


def tqUsage(args: List) -> None:
    '''
    Print TQ Usage
    '''
    usage = f'''
Usage: {args[0]} ACTION [COMMAND_ARGS]
       {args[0]} [OPTION] -- TASK

Description:
    TQ (Task Queue) is a simple Command Line Job Manager. In principle TQ is
    a very flexible and smart atd(8), which could arrange a series of jobs in
    an efficient way.
    (1) By default TQ will run the jobs one by one in the FIFO order.
    (2) A job with high priority will be processed earlier.
    (3) Given the estimated occupancy coefficient, jobs can be executed in
        parallel as long as possible.
    The management of job queue is based on SQLite3 database, in which
    information about every job, including the start and end time, is stored.

Available Actions:
    start      start TQ's daemon
    stop       stop TQ's daemon
    log        dump log to screen
    ls         fancy print of task queue
    db         print database content to screen
    pri ID P   change prioritry of task ID
    rsc ID R   change resource occupance of task ID
    rm <ID>    remove task with specified id, see ID with tq ls
    clean      remove finished tasks from queue
    purge      remove log file and sqlite3 db file

Apending Task:
    -- TASK        append TASK to the queue
    p<P> -- TASK   append TASK with priority P to the queue
    r<R> -- TASK   append TASK with resource occupancy R to the queue
    P R -- TASK    append TASK with priority P and estimated occupancy R
                   int P default  0 range [INT_MIN, INT_MAX], large=important
                   int R detault 10 range [1,       10],      large=consuming

Examples:
    1. Serial: the two given tasks should be executed one by one
         tq -- sleep 100
         tq -- sleep 100
    2. Parallel: each task occupies 40% of resource.
       In this example two tasks will be active at the same time.
         tq r4 -- sleep 100
         tq r4 -- sleep 100
         tq r4 -- sleep 100
    3. Priority: break the FIFO order of tasks. 1 > default Priority.
         tq p1 -- sleep 100
    4. Special Case: run the given task right away ignoring Pri and Rsc
         tq 1 0 -- sleep 100
        '''
    print(usage)


# Foreground color, normal
def red(x): return re.sub('^(.*)$', '\033[0;31m\\1\033[;m', x)
def green(x): return re.sub('^(.*)$', '\033[0;32m\\1\033[;m', x)
def yellow(x): return re.sub('^(.*)$', '\033[0;33m\\1\033[;m', x)
def blue(x): return re.sub('^(.*)$', '\033[0;34m\\1\033[;m', x)
def purple(x): return re.sub('^(.*)$', '\033[0;35m\\1\033[;m', x)
def cyan(x): return re.sub('^(.*)$', '\033[0;36m\\1\033[;m', x)
def white(x): return re.sub('^(.*)$', '\033[0;37m\\1\033[;m', x)
# Foreground color, bold
def Red(x): return re.sub('^(.*)$', '\033[1;31m\\1\033[;m', x)
def Green(x): return re.sub('^(.*)$', '\033[1;32m\\1\033[;m', x)
def Yellow(x): return re.sub('^(.*)$', '\033[1;33m\\1\033[;m', x)
def Blue(x): return re.sub('^(.*)$', '\033[1;34m\\1\033[;m', x)
def Purple(x): return re.sub('^(.*)$', '\033[1;35m\\1\033[;m', x)
def Cyan(x): return re.sub('^(.*)$', '\033[1;36m\\1\033[;m', x)
def White(x): return re.sub('^(.*)$', '\033[1;37m\\1\033[;m', x)
# background color
def RedB(x): return re.sub('^(.*)$', '\033[1;41m\\1\033[;m', x)
# other control sequences
def Tset(x): return re.sub('^(.*)$', '\0337\\1', x)  # store location
def Tcls(x): return re.sub('^(.*)$', '\033[K\\1', x)  # clear line
def Tres(x): return re.sub('^(.*)$', '\\1\0338', x)  # restore location 
def underline(x): return re.sub('^(.*)$', '\033[4m\\1\033[24m', x)


def sec2hms(s: float) -> str:
    '''
    Convert X seconds into A hour B minute C seconds as a string.
    '''
    sec = math.fmod(s, 60.0)
    mm  = (int(s) // 60) % 60
    hh  = (int(s) // 60) // 60
    return f'{hh}h{mm}m{sec:.3f}s'


def dbExec(dbpath: str, sql: str) -> None:
    '''
    Execute a SQL statement on a given DB file.
    '''
    conn = sqlite3.connect(dbpath)
    conn.execute(sql)
    conn.commit()
    conn.close()


def dbQuery(dbpath: str, sql: str) -> List:
    '''
    Query from DB
    '''
    tq = sqlite3.connect(dbpath)
    cursor = tq.cursor()
    cursor.execute(sql)
    values = cursor.fetchall()  # len(values) may be 0
    cursor.close()
    tq.close()
    return values


def tqCreateDB(dbpath: str) -> None:
    '''
    Create a sqlite3 database for Tq Daemon use.
    '''
    sql = 'create table tq (id, pid, cwd, cmd, retval, stime, etime, pri, rsc)'
    dbExec(dbpath, sql)


def sql_pretty(sql):
    '''
    SQL statement formatter
    '''
    ret = sql
    pre = '    sqlite3 operation ...'
    prompt = '    sqlite3>  '
    prompt2 = '    sqlite3.. '
    ret = ret.replace('select', pre+'\n'+prompt+'select')
    ret = ret.replace('update', pre+'\n'+prompt+'update')
    ret = ret.replace('insert', pre+'\n'+prompt+'insert')
    ret = ret.replace('values', '\n'+prompt2+'values')
    ret = ret.replace('where', '\n'+prompt2+'where')
    ret = ret.replace('and', '\n'+prompt2+'and')
    ret = ret.replace('or', '\n'+prompt2+'or')
    return ret


def daemonize(*, uid, pidfile,
              stdin='/dev/null', stdout='/dev/null', stderr='/dev/null'):
    '''
    Turn into a Daemon
    '''

    if os.path.exists(pidfile):
        raise RuntimeError('Already running')

    # First fork (detaches from parent)
    try:
        if os.fork() > 0:
            raise SystemExit(0)   # Parent exit
    except OSError as e:
        raise RuntimeError('fork #1 failed.')

    os.chdir('/')
    os.umask(0)
    os.setsid()
    # Second fork (relinquish session leadership)
    try:
        if os.fork() > 0:
            raise SystemExit(0)
    except OSError as e:
        raise RuntimeError('fork #2 failed.')

    # Flush I/O buffers
    sys.stdout.flush()
    sys.stderr.flush()

    # Replace file descriptors for stdin, stdout, and stderr
    with open(stdin, 'rb', 0) as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open(stdout, 'ab', 0) as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
    with open(stderr, 'ab', 0) as f:
        os.dup2(f.fileno(), sys.stderr.fileno())

    # Write the PID file
    os.system('touch {}'.format(pidfile))
    with open(pidfile, 'w+') as f:
        print(os.getpid(), file=f)

    # Arrange to have the PID file removed on exit/signal
    atexit.register(lambda: os.remove(pidfile))

    # Signal handler for termination (required)
    def sigterm_handler(signo, frame):
        log.info('TQD recieved SIGTERM, exit.')
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, sigterm_handler)


def _tqWorker(dbpath: str, task: tuple) -> None:
    '''
    Process a task.
    (id, pid, cwd, cmd, retval, stime, etime, pri, rsc)
    '''
    task = (x if x is not None else 'null' for x in task)
    id_, pid, cwd, cmd, retval, stime, etime, pri, rsc = task
    pid = os.getpid()

    # update database before working
    sql = f"update tq set pid = {pid}, stime = {time.time()}" \
        + f" where (id = {id_}) limit 1"
    log.debug(f'Worker[{os.getpid()}]: SQL update -- {sql}')
    dbExec(dbpath, sql)

    try:
        # change directory, fork and execute the task.
        os.chdir(cwd)
        proc = subprocess.Popen(
            shlex.split(cmd), shell=False, stdin=None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)

        # override TQD's sigaction handler
        def sigterm_handler(signo, frame):
            log.info(f'Worker[{os.getpid()}] recieved SIGTERM. Gracefully pulling down task process...')
            proc.kill()
            os._exit(0)
        signal.signal(signal.SIGTERM, sigterm_handler)

        # If nothing goes wrong, we'll get the content of stdout and stderr
        tqout, tqerr = proc.communicate()
        retval = proc.returncode
    except FileNotFoundError as e:
        log.error('    {}'.format(str(e)))
        tqout, tqerr = '', ''
        retval = -1
    except Exception as e:
        log.error(f'    {e}')
        tqout, tqerr = '', ''
        retval = -1
    finally:
        log.info(f'Worker[{os.getpid()}]: subprocess.Popen() task successfully returned.')

    os.chdir(cwd)
    if len(tqout) > 0:
        with open('tq.out', 'a+') as f:
            f.write(tqout.decode())
    if len(tqerr) > 0:
        with open('tq.err', 'a+') as f:
            f.write(tqerr.decode())

    # update database after finishing the task
    sql = f"update tq set retval = {retval}, etime = {time.time()}," \
        + f"pid = null where (id = {id_}) limit 1"
    log.debug(f'Worker[{os.getpid()}]: SQL update -- {sql}')
    dbExec(dbpath, sql)
    # don't remove pidfile! i.e. don't trigger atexit().
    os._exit(0)


def _tqWPrefresh(workerpool: List) -> List:
    '''
    Refresh a worker pool, removing dead workers. Return a clean workerpool.
    '''
    _wp = []
    for w in workerpool:
        if w.is_alive():
            _wp.append(w)
        else:
            w.join(timeout=3)
            w.terminate()
    return _wp


def _tqDaemon(dbpath: str, pidfile: str) -> None:
    '''
    Tq's Daemon, the scheduler
    '''
    log.info(f'TQD started with pid {os.getpid()}')

    if not os.path.exists(dbpath):
        log.info(f'TQD is creating a new SQLite3 databse')
        tqCreateDB(dbpath)
    log.debug('TQD is keeping an eye on SQLite3 databse ...')

    def _daemonsleep() -> None:
        time.sleep(1)

    workerpool = []
    while True:

        # Assessment: find the current available resource coefficient
        sql = f'select rsc from tq where (pid is not null) and (pid > 0)'
        arsc = 10 - sum(x[0] for x in dbQuery(dbpath, sql))

        # Assessment: find the current highest priority among running jobs
        sql = f'select pri from tq where (pid is not null) and (pid > 0)'
        results = dbQuery(dbpath, sql)
        curhighpri = max(x[0] for x in results) if len(results)>0 else 0

        # Tier 1 candidate: is there any high-pri candidate waiting in queue?
        sql = f'select id from tq where (pid is null) and (retval is null) and (pri > {curhighpri}) order by pri desc'
        results = dbQuery(dbpath, sql)
        if len(results) > 0:
            sql = f'select * from tq where (pid is null) and (retval is null) and (pri > {curhighpri}) and (rsc <= {arsc}) order by pri desc'
            todolist = dbQuery(dbpath, sql)
        else:
            # Tier 2 candidates: equal priority tasks
            sql = f'select id from tq where (pid is null) and (retval is null) and (pri = {curhighpri}) order by id'
            results = dbQuery(dbpath, sql)
            if len(results) > 0:
                sql = f'select * from tq where (pid is null) and (retval is null) and (pri = {curhighpri}) and (rsc <= {arsc}) order by id'
                todolist = dbQuery(dbpath, sql)
            else:
                # Tier 3 candidates: lower priority
                sql = f'select * from tq where (pid is null) and (retval is null) and (rsc <= {arsc}) order by pri desc, id'
                todolist = dbQuery(dbpath, sql)

        if len(todolist) > 0:  # there are works to do
            task = todolist[0]
            log.debug(f'TQD[{os.getpid()}]: Next task scheduled -- {task}')
            id_, pid, cwd, cmd, retval, stime, etime, pri, rsc = task

            log.info(f'About to execute the next task -- {task}')
            log.info('    cwd = {}'.format(cwd))
            log.info('    cmd = {}'.format(cmd))

            # create a new worker process for this task
            worker = mp.Process(target=_tqWorker, args=(dbpath, task))
            workerpool.append(worker)
            worker.start()

        # domestic stuff
        _daemonsleep()
        workerpool = _tqWPrefresh(workerpool)


def tqStart(pidfile, sqlite, logfile) -> None:
    '''
    Start the Tq Daemon for Task scheduling
    '''
    log.info('starting Tqd ...')
    try:
        daemonize(uid=os.getuid(),
                  pidfile=pidfile,
                  stdout=logfile,
                  stderr=logfile)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
    _tqDaemon(sqlite, pidfile)


def _tqCheckPID(pid: int) -> bool:
    '''
    Check if a given process specified by its PID is alive.
    '''
    try:
        os.kill(pid, 0)  # does nothing
    except OSError:
        return False
    else:
        return True


def _tqCheckAlive(pidfile: str) -> bool:
    '''
    Check if TQD is alive given the pidfile.
    '''
    if os.path.exists(pidfile):
        with open(pidfile) as f:
            pid = int(f.read())
        if _tqCheckPID(pid):
            return True
        else:
            # process does not exist
            os.remove(pidfile)
            return False


def _tqCheckWorkerAlive(dbpath: str) -> bool:
    '''
    Check if the "running" workers are alive.
    Set pid to -1 to indicate abnormal behaviour.
    '''
    if os.path.exists(dbpath):
        sql = 'select id, pid from tq where (not pid is null) and (retval is null)'
        workers = dbQuery(dbpath, sql)
        wkstat = [_tqCheckPID(pid) for id_, pid in workers]
        if all(wkstat):
            return True
        else:
            xids = [workers[i][0] for i, st in enumerate(wkstat) if not st]
            for id_ in xids:
                sql = f'update tq set pid = {-1} where (id = {id_}) limit 1'
                dbExec(dbpath, sql)
            return False
    else:
        return True


def tqStop(pidfile: str) -> None:
    '''
    Stop the Daemon
    '''
    if os.path.exists(pidfile):
        with open(pidfile) as f:
            os.kill(int(f.read()), signal.SIGTERM)
    else:
        log.info('TQD is NOT running...')
        raise SystemExit(1)


def tqKill(pidfile: str, dbpath: str, id_: int) -> None:
    '''
    Kill the worker specified by given task id
    '''
    if os.path.exists(dbpath):
        sql = f'select pid from tq where (id = {id_}) limit 1'
        tpid = dbQuery(dbpath, sql)[0][0]
        log.info(f'TQ: Requested to kill pid {tpid}')
        if _tqCheckPID(tpid):
            os.kill(tpid, signal.SIGTERM)


def tqLs(pidfile: str, dbpath: str) -> None:
    '''
    List items in the tq database in pretty format. Fancy version of tqDumpDB
    '''
    if not os.path.exists(dbpath):
        log.error('Oops! TQ database is not found.')
        return
    sql = 'select * from tq'
    tasks = dbQuery(dbpath, sql)
    print(yellow('┌───┬'+'─'*73+'┐'))
    for k, task in enumerate(tasks, 1):
        id_, pid, cwd, cmd, retval, stime, etime, pri, rsc = task
        if retval is None and pid is None:  # wait
            status = white('[♨]')
        elif retval is not None and pid is None:
            status = Green('[✓]') if retval == 0 else RedB(White(f'[✗ {retval}]'))
        elif retval is None and pid is not None:
            status = Cyan(f'[⚙ {pid}]') if pid > 0 else Yellow(f'[⚠ Accident]')
        else:
            status = redB('[???BUG???]')
        # first line : status
        print(Yellow(f'│{id_:>3d}│'), 'St', status,
              f'| Pri {Purple("-" if 0==pri else str(pri))}',
              f'| Rsc {Purple("-" if 10==rsc else str(rsc))}')
        # second line : time
        if all((stime, etime)):
            print(yellow('│   ├'), Yellow('☀'), Purple(f'{sec2hms(etime-stime)}'),
                  f'| ({time.ctime(stime)}) ➜ ({time.ctime(etime)})')
        elif stime:
            print(yellow('│   ├'), Yellow('☀'), f'Started at ({time.ctime(stime)})',
                  Purple(f'➜ {sec2hms(time.time() - stime)}'), 'Elapsed.')
        # third line: cwd
        print(yellow('│   ├'), Yellow('⚑'), Blue(cwd))
        prog, args = cmd.split()[0], ' '.join(cmd.split()[1:])
        # fourth line: cmd
        print(yellow('│   ├'), Yellow('✒'), purple(underline(prog)), green(underline(args)))
        print(yellow('├───┼'+'─'*73+'┤'))
    # print summary
    sql = 'select id from tq where not (pid is null) and (pid > 0)'
    stat_running = len(dbQuery(dbpath, sql))
    sql = 'select id from tq where (pid is null) and (retval is null)'
    stat_wait = len(dbQuery(dbpath, sql))
    sql = 'select id from tq where (pid is null) and not (retval is null)'
    stat_done = len(dbQuery(dbpath, sql))
    sql = 'select id from tq where not (pid is null) and (pid < 0)'
    stat_accident = len(dbQuery(dbpath, sql))
    sql = f'select rsc from tq where (pid is not null) and (pid > 0)'
    arsc = 10 - sum(x[0] for x in dbQuery(dbpath, sql))

    print(yellow('│ ') + White('❄') + yellow(' │'),
          f'Stat: {stat_running:>2d} Running, {stat_wait:>2d} Waiting,',
          f'{stat_done:>2d} Done, {stat_accident:>2d} Accident,',
          f'{arsc:>2d} Rsc Avail.', '    ', yellow(' │'))
    print(yellow('└───┴'+'─'*73+'┘'))


def tqPurge(pidfile: str, dbpath: str, logfile: str,
            really: bool = False) -> None:
    '''
    Cleanup the database, optionally remove all tq files.
    '''
    # cleanup entries in the database
    if os.path.exists(dbpath):
        sql = 'delete from tq where retval is not null'
        dbExec(dbpath, sql)
    # remove all related file if tqd is not running
    if really and not os.path.exists(pidfile):
        print(Tres(Tset('[..] Purging Tq database and log')), end='')
        if os.path.exists(logfile):
            os.unlink(logfile)
        if os.path.exists(dbpath):
            os.unlink(dbpath)
        print('[OK] Purging Tq database and log')


def tqEnqueue(dbpath: str, *,
              id_: int = 'null', pid='null', cwd: 'null', cmd: str, retval='null',
              stime: int = 'null', etime: int = 'null', pri: int = 0, rsc: int = 10) -> None:
    '''
    Enqueue a task into tq database. One must provide (cwd, cmd)

    id: must, int, task indicator
    pid: opt, None or int or bool, int for PID, None for waiting, bool True for complete
    cwd: must, str
    cmd: must, str
    retval: opt, None or int
    stime: opt, None or long, seconds since epoch, start time
    etime: opt, None or long, seconds since epoch, end time
    pri: opt, None or int, range R_int
    rsc: opt, None or int, range [0-10]
    '''
    ids = [t[0] for t in dbQuery(dbpath, 'select id from tq')]
    id_ = max(ids)+1 if len(ids) > 0 else 1
    sql = f'insert into tq' + \
           ' (id, pid, cwd, cmd, retval, stime, etime, pri, rsc)' + \
           ' values' + \
          f' ({id_}, {pid}, "{cwd}", "{cmd}", {retval}, {stime}, {etime}, {pri}, {rsc})'
    log.info(f'tqEnqueue: {sql_pretty(sql)}')
    dbExec(dbpath, sql)


def tqDequeue(dbpath: str, id_: int) -> None:
    '''
    Remove a task specified by id_ from Tq database.
    Do nothing if pid is not empty for sanity.
    '''
    if os.path.exists(dbpath):
        sql = f'delete from tq where ((pid is null) or (pid < 0)) and (id = {id_}) limit 1'
        log.info(f'TQ SQL update -- {sql}')
        dbExec(dbpath, sql)


def tqDumpDB(dbpath: str) -> None:
    '''
    Dump database to screen. Raw version of tqLS.
    '''
    if os.path.exists(dbpath):
        sql = f'select * from tq'
        tasks = dbQuery(dbpath, sql)
        for x in tasks:
            print(x)


def tqEdit(dbpath: str, id_: int, *, pri: int = 0, rsc: int = 10):
    '''
    editing Pri and Rsc attributes
    '''
    sql = f"update tq set pri = {pri}, rsc = {rsc}" \
        + f" where (id = {id_}) limit 1"
    log.debug(f'tqEdit SQL update -- {sql}')
    dbExec(dbpath, sql)


def tqMain():
    '''
    tq's main func. It parses command line argument and invoke specified
    functions of tq.
    '''
    import logging as log
    log.basicConfig(
        format='%(levelno)s %(asctime)s %(process)d %(filename)s:%(lineno)d]'
        + ' %(message)s',
        level=log.DEBUG
    )

    # get some info and paths
    uid, cwd = os.getuid(), os.getcwd()
    sqlite  = os.path.expanduser(f'~/.tqd_{uid}.db')
    logfile = os.path.expanduser(f'~/.tqd_{uid}.log')
    pidfile = os.path.expanduser(f'~/.tqd_{uid}.pid')

    _tqCheckAlive(pidfile)
    _tqCheckWorkerAlive(sqlite)

    if len(sys.argv) < 2:
        tqUsage(sys.argv)
        if os.path.exists(pidfile):
            print('TQ daemon is \x1b[32;1mrunning\x1b[m.')
        else:
            print('TQ daemon is \x1b[31;1mnot running\x1b[m.')
        raise SystemExit(1)

    if sys.argv[1] == 'start':
        tqStart(pidfile, sqlite, logfile)

    elif sys.argv[1] == 'stop':
        tqStop(pidfile)

    elif sys.argv[1] == 'log':
        if os.path.exists(logfile):
            with open(logfile, 'r') as log:
                print(log.read())

    elif sys.argv[1] == 'clean':
        tqPurge(pidfile, sqlite, logfile, False)

    elif sys.argv[1] == 'purge':
        tqPurge(pidfile, sqlite, logfile, True)

    elif sys.argv[1] == 'ls':
        tqLs(pidfile, sqlite)

    elif sys.argv[1] == 'rm':
        tqDequeue(sqlite, int(sys.argv[2]))

    elif sys.argv[1] == 'kill':
        tqKill(pidfile, sqlite, int(sys.argv[2]))

    elif sys.argv[1] == 'db':
        tqDumpDB(sqlite)

    elif sys.argv[1] == '_check':
        print('check TQ daemon ...', _tqCheckAlive(pidfile))
        print('check Workers   ...', _tqCheckWorkerAlive(sqlite))

    elif len(sys.argv) == 4 and sys.argv[1] == 'pri':
        tqEdit(sqlite, int(sys.argv[2]), pri=int(sys.argv[3]))

    elif len(sys.argv) == 4 and sys.argv[1] == 'rsc':
        tqEdit(sqlite, int(sys.argv[2]), rsc=int(sys.argv[3]))

    elif len(sys.argv) >= 5 and sys.argv[3] == '--':
        # Special and powerful mode: specify priority and resource requirement
        pri, rsc = int(sys.argv[1]), int(sys.argv[2])
        cwd = os.getcwd()
        cmd = ' '.join(sys.argv[4:])

        if len(cmd) == 0:
            log.error('Task missing')
            raise SystemExit(1)

        if not os.path.exists(pidfile):
            log.error('Tqd is not running, start tqd first.')
        else:
            tqEnqueue(sqlite, cwd=cwd, cmd=cmd, pri=pri, rsc=rsc)

    elif len(sys.argv) >= 4 and sys.argv[2] == '--':
        # specify P or R
        if sys.argv[1].startswith('p') or sys.argv[1].startswith('P'):
            pri, rsc = int(sys.argv[1][1:]), 10
        elif sys.argv[1].startswith('r') or sys.argv[1].startswith('R'):
            pri, rsc = 0, int(sys.argv[1][1:])
        else:
            raise Exception('Unexpected argv[1]')

        cwd, cmd = os.getcwd(), ' '.join(sys.argv[3:])
        if len(cmd) == 0:
            log.error('Task Missing')
            raise SystemExit(1)

        if not os.path.exists(pidfile):
            log.error('Tqd is not running, start tqd first.')
        else:
            tqEnqueue(sqlite, cwd=cwd, cmd=cmd, pri=pri, rsc=rsc)

    elif sys.argv[1] == '--':
        cwd = os.getcwd()
        cmd = ' '.join(sys.argv[2:])

        if len(cmd) == 0:
            log.error('Task missing')
            raise SystemExit(1)

        if not os.path.exists(pidfile):
            log.error('Tqd is not running, start tqd first.')
        else:
            tqEnqueue(sqlite, cwd=cwd, cmd=cmd)

    else:
        log.error('Unknown command {!r}'.format(sys.argv[1]))
        raise SystemExit(1)


main = tqMain


if __name__ == '__main__':
    tqMain()
