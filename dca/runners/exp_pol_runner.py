import logging
import sys
from functools import partial
from multiprocessing import Process, Queue, cpu_count
from operator import itemgetter

import numpy as np

from runners.runner import Runner


class ExpPolRunner(Runner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def run(self):
        pols = {
            # 'boltzmann': {'epsilon': [2, 5, 10]},
            # 'nom_boltzmann': {'epsilon': [2, 5, 10]},
            # 'eps_greedy': {'epsilon': [0.0, 0.2, 0.4, 0.7]},
            # 'nom_eps_greedy': {'epsilon': [0.1, 0.4, 0.7]},
            # 'nom_greedy': {'epsilon': [0]},
            # 'nom_fixed_greedy': {'epsilon': [0]},
            'bgumbel': {'exp_policy_param': [1.0, 4.0, 4.5, 5.0, 5.5, 10.0]}
        }  # yapf: disable
        space, results = [], []
        for pol, polparams in pols.items():
            for param, pvals in polparams.items():
                for pval in pvals:
                    space.append({'exp_policy': pol, param: pval})
                    results.append({'exceeded_btresh': False, 'results': []})
        n_avg = self.pp['exp_policy_cmp']
        # n_concurrent = cpu_count() // 2
        n_concurrent = cpu_count() - 1
        self.logger.error(
            f"Running {n_concurrent} concurrent procs with {n_avg} average runs "
            f"for up to {n_avg*len(space)} sims on space:\n{pols}")
        result_queue = Queue()
        simproc = partial(exp_proc, self.stratclass, self.pp, result_queue)
        # If the first run of a set of params exceeds block prob, there's
        # no need to run multiple of them and take the average.

        def print_results():
            for evaluation in results:
                res = evaluation['results']
                mean = np.mean(res) if len(res) > 0 else 1
                evaluation['avg_result'] = mean
            params_and_res = [{**p, **r} for p, r in zip(space, results)]
            self.logger.error("\n".join(map(repr, params_and_res)))
            best = max(params_and_res, key=itemgetter('avg_result'))
            self.logger.error(f"Best:\n{best}")

        def spawn_eval(i):
            j = i % len(space)
            if not results[j]['exceeded_btresh']:
                Process(target=simproc, args=(i, space[j])).start()
                return True
            return False

        def store_result():
            try:
                # Blocks until a result is ready
                i, result = result_queue.get()
            except KeyboardInterrupt:
                print_results()
                sys.exit(0)
            else:
                j = i % len(space)
                if result is None:
                    results[j]['exceeded_btresh'] = True
                else:
                    results[j]['results'].append(result)

        for i in range(n_concurrent - 1):
            spawn_eval(i)
        for i in range(n_concurrent - 1, n_avg * len(space)):
            did_spawn = spawn_eval(i)
            if did_spawn:
                store_result()
        for _ in range(n_concurrent - 1):
            store_result()
        print_results()


def exp_proc(stratclass, pp, result_queue, i, space):
    logger = logging.getLogger('')
    logger.error(f"T{i} Testing {space}")
    for param, paramval in space.items():
        pp[param] = paramval
    np.random.seed()
    strat = stratclass(pp=pp, logger=logger, pid=i)
    res = strat.simulate()[0]
    # if strat.quit_sim and not strat.invalid_loss and not strat.exceeded_bthresh:
    if strat.quit_sim:
        # If user quits sim or sim exceeded block thresh, don't store result
        res = None
    else:
        assert res is not None
        # Must negate result as dlib performs maximization by default
        res = -res
    result_queue.put((i, res))