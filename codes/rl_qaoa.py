import pennylane as qml
from pennylane import numpy as np
import torch
import copy
from tqdm import tqdm
from scipy.optimize import minimize
import torch
from codes.data_process import Tree,off_diagonal_median,zero_lower_triangle,ising_to_qubo,qubo_to_ising,plot_rl_qaoa_results
from codes.pulse_simulator import Pulse_simulation_fixed


class RL_QAOA:
    """
    A reinforcement learning-based approach to solving QAOA (Quantum Approximate Optimization Algorithm)
    for quadratic unconstrained binary optimization (QUBO) problems.

    Parameters
    ----------
    Q : np.ndarray
        QUBO matrix representing the optimization problem.

    n_c : int
        The threshold number of nodes at which classical brute-force optimization is applied.

    init_paramter : np.ndarray
        Initial parameters for the QAOA circuit.

    b_vector : np.ndarray
        The beta vector used in reinforcement learning to guide edge selection.

    QAOA_depth : int
        Depth of the QAOA circuit, representing the number of layers.

    gamma : float, default=0.99
        Discount factor used in reinforcement learning.

    learning_rate_init : float, default=0.001
        Initial learning rate for the Adam optimizer.

    Attributes
    ----------
    qaoa_layer : QAOA_layer
        Instance of the QAOA layer with specified depth and QUBO matrix.

    optimizer : AdamOptimizer
        Adam optimizer instance to optimize QAOA parameters.

    same_list : list
        List of edges that should have the same value.

    diff_list : list
        List of edges that should have different values.

    node_assignments : dict
        Tracks assigned values to the nodes.

    """

    def __init__(self, qubo, n_c, init_paramter, b_vector, QAOA_depth, gamma=0.99, learning_rate_init=[0.01,0.05],ising = False):
        if ising:
            Q = qubo
        else:
            Q = qubo_to_ising(qubo)
        self.Q = Q
        self.n_c = n_c
        self.param = init_paramter
        self.b = b_vector
        self.p = QAOA_depth
        self.qaoa_layer = QAOA_layer(QAOA_depth, Q)
        self.gamma = gamma
        self.optimzer = AdamOptimizer([init_paramter, b_vector], learning_rate_init=learning_rate_init)
        self.lr = learning_rate_init
        self.tree = Tree('root',None)
        self.tree_grad = Tree('root',None)

    def RL_QAOA(self, episodes, epochs,log_interval = 5, correct_ans=None):
        self.avg_values = []
        self.min_values = []
        self.prob_values = []
        self.best_states = []
        self.best_same_lists = []
        self.best_diff_lists = []

        """
        Performs the reinforcement learning optimization process with progress tracking.

        Parameters
        ----------
        episodes : int
            Number of Monte Carlo trials for the optimization.

        epochs : int
            Number of optimization iterations to update parameters.

        correct_ans : float, optional
            The correct optimal solution (if available) to calculate success probability.
        """

        for j in range(epochs):

            if self.lr[0] != 0:
                num = self.tree.node_num

                self.tree = Tree('root',None)
                self.tree.node_num = num
                self.tree_grad = Tree('root',None)
                self.tree_grad.node_num = num
            value_list = []
            state_list = []
            QAOA_diff_list = []
            beta_diff_list = []
            same_lists = []
            diff_lists = []

            if correct_ans is not None:
                prob = 0

            # Progress bar for episodes within the current epoch
            for i in tqdm(range(episodes), desc=f'Epoch {j + 1}/{epochs}', unit=' episode'):
                QAOA_diff, beta_diff, value, final_state, same_list, diff_list = self.rqaoa_execute()
                value_list.append(value)
                state_list.append(final_state)
                same_lists.append(same_list)
                diff_lists.append(diff_list)
                QAOA_diff_list.append(QAOA_diff)
                beta_diff_list.append(beta_diff)

                if correct_ans is not None and correct_ans - 0.01 <= value <= correct_ans + 0.01:
                    prob += 1

            # Compute softmax rewards and normalize

            batch_mean = (np.array(value_list) - np.mean(value_list))
            #batch_plus = np.where(batch_mean < 0, batch_mean, 0)
            #softmaxed_rewards = signed_softmax_rewards(batch_plus, beta=15)*episodes
            for index, val in enumerate(batch_mean):
                QAOA_diff_list[index] *= -batch_mean[index]
                beta_diff_list[index] *= -batch_mean[index]

            # Compute parameter updates
            QAOA_diff_sum = np.mean(QAOA_diff_list, axis=0)
            beta_diff_sum = np.mean(beta_diff_list, axis=0)
            value_sum = np.mean(value_list)
            min_value = np.min(value_list)  # Find the lowest reward value
            min_index = np.argmin(value_list)  # Index of lowest reward value
            # Store values
            self.avg_values.append(value_sum)
            self.min_values.append(min_value)
            if correct_ans is not None:
                prob /= episodes
                self.prob_values.append(prob)
            self.best_states.append(state_list[min_index])
            self.best_same_lists.append(same_lists[min_index][:3])  # Store top 3 same list elements
            self.best_diff_lists.append(diff_lists[min_index][:3])  # Store top 3 diff list elements

            # Print optimization progress
            if j % log_interval == 0:
                if correct_ans is not None:
                    print(f'  Probability of finding correct solution: {prob:.4f}')
                print(f'  Average reward: {value_sum}')
                print(f'  Lowest reward obtained: {min_value}')
                print(f'  Best state at lowest value: {self.best_states[-1]}')
                print(f'  number of nodes : {self.tree.node_num}')
                #print(f'  Top 3 same constraints: {self.best_same_lists[-1]}')
                #print(f'  Top 3 different constraints: {self.best_diff_lists[-1]}')


            # Update parameters using the Adam optimizer
            update = self.optimzer.get_updates([QAOA_diff_sum, beta_diff_sum])
            self.param += np.array(update[0])
            self.b += np.array(update[1])

    def rqaoa_execute(self, cal_grad=True):
        """
        Executes the RQAOA algorithm by iteratively reducing the QUBO problem.

        Parameters
        ----------
        cal_grad : bool, default=True
            Whether to calculate the gradient.

        Returns
        -------
        tuple or float
            If cal_grad is True, returns gradients, value, and final state.
            Otherwise, returns only the final value.
        """

        Q_init = copy.deepcopy(self.Q)
        Q_action = copy.deepcopy(self.Q)
        self.same_list = []
        self.diff_list = []
        self.node_assignments = {}
        self.edge_expectations = []
        self.edge_expectations_grad = []
        self.policys = []

        QAOA_diff_list = []
        beta_diff_list = []
        index = 0




        while Q_init.shape[0] > self.n_c:
            Q_init = zero_lower_triangle(Q_init)/off_diagonal_median(zero_lower_triangle(Q_init)) * 1 ## Normalization
            if self.b.ndim == 1:
                self.beta = self.b
            else:
                self.beta = self.b[index]


            if self.tree.state.value is None:
                edge_expectations = self._qaoa_edge_expectations(
                    Q_init, [i for i in range(self.p * index * 2, self.p * index * 2 + 2 * self.p)]
                )
                self.tree.state.value = edge_expectations
            else:
                edge_expectations = self.tree.state.value
            selected_edge_idx, policy, edge_res = self._select_edge_to_cut(Q_action, edge_expectations)

            if cal_grad:
                """ edge_res_grad = self._qaoa_edge_expectations_gradient(
                    Q_init, [i for i in range(self.p * index * 2, self.p * index * 2 + 2 * self.p)], selected_edge_idx
                ) """
                if self.lr[0] != 0:
                    if self.tree_grad.state.value is None:
                        edge_res_grad = self._qaoa_edge_expectations_gradients(
                            Q_init, [i for i in range(self.p * index * 2, self.p * index * 2 + 2 * self.p)]
                        )
                        self.tree_grad.state.value = edge_res_grad
                        self._tree_action(self.tree_grad, edge_expectations,selected_edge_idx,Q_init)

                    else:
                        edge_res_grad = self.tree_grad.state.value
                        self._tree_action(self.tree_grad, edge_expectations,selected_edge_idx,Q_init)



                if self.lr[0] != 0:
                    QAOA_diff = self._compute_log_pol_diff(
                        selected_edge_idx, Q_action, edge_res, edge_res_grad, policy
                    ) * self.gamma ** (Q_init.shape[0] - index)

                else:
                    QAOA_diff = np.zeros_like(self.param)

                beta_diff = self._compute_grad_beta(selected_edge_idx, Q_action, policy, edge_res) * self.gamma ** (Q_init.shape[0] - index)
                QAOA_diff_list.append(QAOA_diff)
                beta_diff_list.append(beta_diff)

            Q_init, Q_action = self._cut_edge(selected_edge_idx, edge_res, Q_action, Q_init)
            index += 1

        self.tree.reset_state()
        self.tree_grad.reset_state()
        # Solve smaller problem using brute force
        self._brute_force_optimal()
        Value = self._state_energy(np.array(self.node_assignments), self.Q)

        # Copy lists to preserve their state
        same_list_copy = copy.deepcopy(self.same_list)
        diff_list_copy = copy.deepcopy(self.diff_list)

        if self.n_c != self.Q.shape[0]:
            QAOA_diff = np.sum(QAOA_diff_list, axis=0)
        else:
            QAOA_diff = None
        if self.n_c != self.Q.shape[0]:
            if self.b.ndim == 1:
                beta_diff = np.sum(beta_diff_list, axis=0)
            else:
                beta_diff = np.stack(beta_diff_list, axis=0)
        else:
            beta_diff = None



        # If gradient calculation is enabled, return additional data
        if cal_grad:
            return QAOA_diff, beta_diff, Value, np.array(self.node_assignments), same_list_copy, diff_list_copy
        else:
            return Value

    def _select_edge_to_cut(self, Q_action, edge_expectations):
        """
        Selects an edge to be cut based on a softmax probability distribution over interactions.

        Parameters
        ----------
        Q_action : np.ndarray
            Current QUBO matrix tracking active nodes.


        edge_expectations : list
            Expectation values of ZZ interactions for all edges.

        Returns
        -------
        tuple
            Index of selected edge, probability distribution, expectation values.
        """
        action_space = self._action_space(Q_action)

        try:
            #value = abs(np.array(edge_expectations))

            #value = value - np.amax(value)
            interactions = abs(np.array(edge_expectations)) * self.beta[action_space]
            #interactions -= np.amax(interactions)
        except:
            print(abs(np.array(edge_expectations)), self.b[action_space])
            raise ValueError("Invalid input", action_space, abs(np.array(edge_expectations)))
        max_value = np.max(interactions)
        safe_interactions = interactions - max_value
        exp_interactions = np.exp(safe_interactions)
        probabilities = exp_interactions/np.sum(exp_interactions)
        #probabilities = torch.softmax(torch.tensor(interactions), dim=0).numpy()
        selected_edge_idx = np.random.choice(len(probabilities), p=probabilities)

        return selected_edge_idx, probabilities, edge_expectations
    def _compute_log_pol_diff(self, idx, Q_action, edge_expectations, edge_expectations_grad, policy):
        """
        Computes the gradient of the log-policy for the selected edge.

        Parameters
        ----------
        idx : int
            Index of the selected edge.

        Q_action : np.ndarray
            QUBO matrix representing the current optimization problem.

        edge_expectations : list
            Expectation values of ZZ interactions for all edges.

        edge_expectations_grad : list
            Gradient of the expectation values of ZZ interactions for all edges.

        policy : list
            Probability distribution over edges for selection.

        Returns
        -------
        np.array
            The computed gradient of the log-policy.
        """
        action_space = self._action_space(Q_action)
        betas = self.beta[action_space]
        gather = np.zeros_like(policy)

        # Compute the weighted sum of policy and betas
        for i in range(len(edge_expectations_grad)):
            gather[i] += policy[i] * betas[i]

        diff_log_pol = betas[idx] * np.sign(edge_expectations[idx]) * edge_expectations_grad[idx]

        # Adjust the gradient with respect to policy values
        for i in range(len(gather)):
            if gather[i]:
                diff_log_pol -= gather[i] * np.sign(edge_expectations[i]) * edge_expectations_grad[i]

        return np.array(diff_log_pol)


    def _compute_grad_beta(self, idx, Q_action, policy, edge_expectations):
        """
        Computes the gradient of the beta parameter.

        Parameters
        ----------
        idx : int
            Index of the selected edge.

        Q_action : np.ndarray
            QUBO matrix representing the current optimization problem.

        policy : list
            Probability distribution over edges for selection.

        edge_expectations : list
            Expectation values of ZZ interactions for all edges.

        Returns
        -------
        np.array
            The computed gradient of the beta parameter.
        """
        abs_expectations = abs(np.array(edge_expectations))
        action_space = self._action_space(Q_action)

        betas_idx = action_space
        grad = np.zeros(len(self.beta))

        grad[betas_idx[idx]] += abs_expectations[idx]

        # Compute gradient by adjusting with policy values
        for i in range(len(action_space)):
            grad[betas_idx[i]] -= policy[i] * abs_expectations[i]

        return np.array(grad)

    def _cut_edge(self, selected_edge_idx, expectations, Q_action, Q_init):
        """
        Cuts the selected edge and returns the reduced QUBO matrix along with a matrix of the same size
        where the corresponding node values are set to zero.

        Parameters
        ----------
        selected_edge_idx : int
            Index of the selected edge to be cut.

        expectations : list
            Expectation values of ZZ interactions for all edges.

        Q_action : np.ndarray
            Current QUBO matrix tracking active nodes.

        Q_init : np.ndarray
            Initial QUBO matrix.

        Returns
        -------
        tuple
            Reduced QUBO matrix and an updated QUBO matrix with the selected nodes set to zero.
        """
        edge_list = [(i, j) for i in range(Q_init.shape[0]) for j in range(Q_init.shape[0]) if Q_init[i, j] != 0 and i != j]
        edge_to_cut = edge_list[selected_edge_idx]
        edge_to_cut = sorted(edge_to_cut)

        expectation = expectations[selected_edge_idx]

        i, j = edge_to_cut[0], edge_to_cut[1]

        for key in dict(sorted(self.node_assignments.items(), key=lambda item: item[0])):
            if i >= key:
                i += 1
            if j >= key:
                j += 1

        self.node_assignments[i] = 1
        self._tree_action(self.tree, expectations, selected_edge_idx, Q_init)
        new_Q, Q_action = reduce_hamiltonian(Q_init, edge_to_cut[0], edge_to_cut[1], self.node_assignments, int(np.sign(expectation)))
        if expectation > 0:
            self.same_list.append((i, j))
        else:
            self.diff_list.append((i, j))


        return new_Q, Q_action


    def _tree_action(self,tree, expectations,selected_edge_idx,Q_init):
        """
        Manages tree-based memoization to avoid redundant quantum computations.

        This function ensures that if a previously computed quantum state is encountered again,
        the stored result is used instead of recomputing via quantum circuits.

        Args:
            tree (Tree): Tree structure storing previously computed states.
            expectations (list): Expectation values for edges.
            selected_edge_idx (int): Index of the edge selected for reduction.
            Q_init (np.ndarray): Initial QUBO matrix before reduction.
        """
        edge_list = [(i, j) for i in range(Q_init.shape[0]) for j in range(Q_init.shape[0]) if Q_init[i, j] != 0 and i != j]
        edge_to_cut = edge_list[selected_edge_idx]
        edge_to_cut = sorted(edge_to_cut)

        expectation = expectations[selected_edge_idx]

        i, j = edge_to_cut[0], edge_to_cut[1]

        for key in dict(sorted(self.node_assignments.items(), key=lambda item: item[0])):
            if i >= key:
                i += 1
            if j >= key:
                j += 1

        if expectation > 0:
            self.key = f'({i},{j})'
            if tree.has_child(self.key):
                tree.move(self.key)

            else:
                tree.create(self.key,None)
                tree.move(self.key)
        else:
            self.key = f'({-i},{-j})'
            if tree.has_child(self.key):
                tree.move(self.key)
            else:
                tree.create(self.key,None)
                tree.move(self.key)


    def _action_space(self, Q_action):
        """
        Maps the edges in the reduced graph to their original positions in the full graph.

        This function is used to track which edges in the reduced graph correspond to the original
        graph's edges after node elimination. When a node is removed, the edge indices in the
        reduced graph will shift, and this function helps maintain consistency with the original
        edge indexing.

        Example:
        --------
        Suppose the original graph has nodes [1, 2, 3, 4, 5] with edges:
            (1,2), (2,3), (3,4), (4,5)

        If node 3 is removed, the reduced graph has edges:
            (1,2), (4,5)

        The reduced graph will renumber nodes as:
            (1,2) -> (1,2), (4,5) -> (2,3)

        This function ensures the correct mapping to the original graph using `Q_action`.

        Parameters
        ----------
        Q_action : np.ndarray
            The original QUBO matrix with node elimination information, used to track active nodes.

        Returns
        -------
        list
            A list of indices indicating which edges in the reduced graph correspond to the original
            graph structure.
        """
        action_space_list = []
        index = 0  # Tracks the original edge indices
        for i in range(Q_action.shape[0]):
            for j in range(i,Q_action.shape[0]):
                if i != j:  # Avoid self-loops
                    if Q_action[i, j] != 0:  # Check if the edge exists in the original graph
                        action_space_list.append(index)  # Store the original edge index
                    index += 1  # Increment index for original edge mapping
        return action_space_list

    def _qaoa_edge_expectations(self, Q, idx):
        """
        Computes the expectation values of ZZ interactions for each edge in the given QUBO matrix.

        Parameters
        ----------
        Q : np.ndarray
            The QUBO matrix representing the optimization problem.

        idx : int
            Index for selecting the QAOA parameters.

        Returns
        -------
        list
            A list of expectation values for ZZ interactions of the edges in the QUBO matrix.
        """
        self.qaoa_layer = QAOA_layer(self.p, Q)

        @qml.qnode(self.qaoa_layer.dev)
        def circuit(param):
            self.qaoa_layer.qaoa_circuit(param)
            return [qml.expval(qml.PauliZ(i) @ qml.PauliZ(j))
                    for i in range(Q.shape[0])
                    for j in range(Q.shape[0])
                    if Q[i, j] != 0 and i != j]

        return circuit(self.param[idx])


    def _qaoa_edge_expectations_gradients(self, Q, idx):
        """
        Computes the gradients of the expectation values of ZZ interactions for each edge.

        Parameters
        ----------
        Q : np.ndarray
            The QUBO matrix representing the optimization problem.

        idx : int
            Index for selecting the QAOA parameters.

        Returns
        -------
        list
            A list of gradient values for the expectation values of ZZ interactions.
        """
        self.qaoa_layer = QAOA_layer(self.p, Q)
        cal_index = []

        @qml.qnode(self.qaoa_layer.dev)
        def circuit(params, cal_list):
            self.qaoa_layer.qaoa_circuit(params[idx])
            return [qml.expval(qml.PauliZ(cal[0]) @ qml.PauliZ(cal[1])) for cal in cal_list]

        # Compute gradients for each valid edge
        for i in range(Q.shape[0]):
            for j in range(Q.shape[0]):
                if Q[i, j] != 0 and i != j:
                    cal_index.append((i,j))




        params = torch.tensor(self.param, requires_grad=True)
        expectation_values = circuit(params,cal_index)
        res = []
        for index in range(len(expectation_values)):
            expectation_values[index].backward(retain_graph= True)
            grad_values = params.grad.clone()  # save gradients
            params.grad.zero_()
            res.append(grad_values)

        return np.array(res,requires_grad=True)



    def _brute_force_optimal(self):
        """
        Finds the optimal solution using brute force when the graph size is small.

        Parameters
        ----------
        Q : np.ndarray
            The reduced QUBO matrix.

        Updates
        -------
        self.node_assignments : dict
            Stores the optimal node assignments obtained through brute-force search.
        """
        n = self.Q.shape[0]
        best_value = np.inf
        res_node = None

        # Find all valid combinations considering the same and different constraints
        comb_list = get_case(self.same_list, self.diff_list,n)

        for comb in comb_list:
            value = self._state_energy(np.array(comb), self.Q)
            if value < best_value:
                best_value = value
                res_node = copy.copy(comb)
        if res_node is None:
            print(f'case : {self.same_list, self.diff_list,n}')
            print(f'result : {get_case(self.same_list, self.diff_list,n)}')
        # Store the optimal assignment
        self.node_assignments = res_node

    def _state_energy(self, state, Q):
        """
        Computes the energy of a given state based on the QUBO matrix.

        Parameters
        ----------
        state : np.ndarray
            Binary state vector (e.g. [-1, 1, -1, 1]).

        Q : np.ndarray
            The QUBO matrix representing the optimization problem.

        Returns
        -------
        float
            The computed energy value of the given state.
        """
        # Create an identity matrix of the same size
        identity_matrix = np.eye(Q.shape[0], dtype=bool)

        # Remove diagonal elements from the QUBO matrix to isolate interactions
        interaction = np.where(identity_matrix, 0, Q)
        diagonal_elements = np.diag(Q)

        # Compute the energy using the QUBO formulation
        value = diagonal_elements @ state + state.T @ interaction @ state
        return value

    def plot_result(self,title = 'RL QAOA'):
        plot_rl_qaoa_results(self.avg_values,self.min_values,self.prob_values,lable=title)



class RL_QAA(RL_QAOA):
    """
    A reinforcement learning-based approach for Quantum Annealing Approximation (QAA)
    to solve quadratic unconstrained binary optimization (QUBO) problems.

    Unlike RL_QAOA, RL_QAA uses an annealing-based reinforcement learning strategy,
    and does not require initial QAOA parameters.

    Parameters
    ----------
    Q : np.ndarray
        QUBO matrix representing the optimization problem.

    n_c : int
        The threshold number of nodes at which classical brute-force optimization is applied.

    b_vector : np.ndarray
        The beta vector used in reinforcement learning to guide edge selection.

    gamma : float, default=0.99
        Discount factor used in reinforcement learning.

    learning_rate_init : float, default=0.05
        Initial learning rate for the Adam optimizer.

    Attributes
    ----------
    pulse : PulseSimulationFixed
        Pulse simulation object used for quantum annealing.

    optimizer : AdamOptimizer
        Adam optimizer instance to optimize QAA parameters.

    tree : Tree
        Tree data structure to store computation history and avoid redundant calculations.

    tree_grad : Tree
        Tree data structure for tracking gradient updates.

    param : np.ndarray
        Parameters for QAA optimization, initialized as [0., 0.].
    """

    def __init__(self, qubo, n_c, b_vector, gamma=0.99, learning_rate_init=0.05):
        self.Q = zero_lower_triangle(qubo_to_ising(qubo))
        self.n_c = n_c
        self.b = b_vector
        self.pulse = Pulse_simulation_fixed(qubo)
        self.gamma = gamma
        self.optimzer = AdamOptimizer([np.array([0.,0]), b_vector], learning_rate_init=[0,learning_rate_init])
        self.lr = [0,learning_rate_init]
        self.tree = Tree('root',None)
        self.tree_grad = Tree('root',None)
        self.param = np.array([0.,0])

    def rqaoa_execute(self):
        """
        Executes the RQAOA algorithm by iteratively reducing the QUBO problem.

        Parameters
        ----------
        cal_grad : bool, default=True
            Whether to calculate the gradient.

        Returns
        -------
        tuple or float
            If cal_grad is True, returns gradients, value, and final state.
            Otherwise, returns only the final value.
        """

        Q_init = copy.deepcopy(self.Q)
        Q_action = copy.deepcopy(self.Q)
        self.same_list = []
        self.diff_list = []
        self.node_assignments = {}
        self.edge_expectations = []
        self.edge_expectations_grad = []
        self.policys = []

        QAOA_diff_list = []
        beta_diff_list = []
        index = 0




        while Q_init.shape[0] > self.n_c:
            Q_init = zero_lower_triangle(Q_init)
            if self.b.ndim == 1:
                self.beta = self.b
            else:
                self.beta = self.b[index]


            if self.tree.state.value is None:
                edge_expectations = self._qaoa_edge_expectations(
                    Q_init
                )
                self.tree.state.value = edge_expectations
            else:
                edge_expectations = self.tree.state.value
            selected_edge_idx, policy, edge_res = self._select_edge_to_cut(Q_action, edge_expectations)



            QAOA_diff = np.zeros_like(self.param)

            beta_diff = self._compute_grad_beta(selected_edge_idx, Q_action, policy, edge_res) * self.gamma ** (Q_init.shape[0] - index)
            QAOA_diff_list.append(QAOA_diff)
            beta_diff_list.append(beta_diff)

            Q_init, Q_action = self._cut_edge(selected_edge_idx, edge_res, Q_action, Q_init)
            index += 1

        self.tree.reset_state()
        self.tree_grad.reset_state()
        # Solve smaller problem using brute force
        self._brute_force_optimal()
        Value = self._state_energy(np.array(self.node_assignments), self.Q)

        # Copy lists to preserve their state
        same_list_copy = copy.deepcopy(self.same_list)
        diff_list_copy = copy.deepcopy(self.diff_list)

        if self.n_c != self.Q.shape[0]:
            QAOA_diff = np.sum(QAOA_diff_list, axis=0)
        else:
            QAOA_diff = None
        if self.n_c != self.Q.shape[0]:
            if self.b.ndim == 1:
                beta_diff = np.sum(beta_diff_list, axis=0)
            else:
                beta_diff = np.stack(beta_diff_list, axis=0)
        else:
            beta_diff = None

        # If gradient calculation is enabled, return additional data
        return QAOA_diff, beta_diff, Value, np.array(self.node_assignments), same_list_copy, diff_list_copy


    def _qaoa_edge_expectations(self, Q):
        """
        Computes the expectation values of ZZ interactions for each edge in the given QUBO matrix.

        Parameters
        ----------
        Q : np.ndarray
            The QUBO matrix representing the optimization problem.

        idx : int
            Index for selecting the QAOA parameters.

        Returns
        -------
        list
            A list of expectation values for ZZ interactions of the edges in the QUBO matrix.
        """
        self.pulse = Pulse_simulation_fixed(ising_to_qubo(Q))
        dev = qml.device("default.qubit", wires=Q.shape[0])
        @qml.qnode(dev)
        def circuit():
            self.pulse.simulate_time_evolution()
            return [qml.expval(qml.PauliZ(i) @ qml.PauliZ(j))
                    for i in range(Q.shape[0])
                    for j in range(Q.shape[0])
                    if Q[i, j] != 0 and i != j]

        return circuit()

    def plot_result(self,title = 'RL QAA'):
        plot_rl_qaoa_results(self.avg_values,self.min_values,self.prob_values,lable=title)



def generate_upper_triangular_qubo(size, node_weight_range=(-3, 3), edge_weight_range=(-3, 3), integer=True, seed=None):
    """
    Generates an upper-triangular QUBO (Quadratic Unconstrained Binary Optimization) matrix.

    Args:
        size (int): The number of variables (size of the QUBO matrix).
        low (int/float): Minimum value of the random elements.
        high (int/float): Maximum value of the random elements.
        integer (bool): If True, generates integer values; otherwise, generates float values.
        seed (int, optional): Random seed for reproducibility.

    Returns:
        np.ndarray: An upper-triangular QUBO matrix of the specified size.
    """
    if seed is not None:
        np.random.seed(seed)

    # Generate random values for the upper triangular part including diagonal
    if integer:
        Q = np.random.randint(edge_weight_range[0], edge_weight_range[1], (size, size))
    else:
        Q = np.random.uniform(edge_weight_range[0], edge_weight_range[1], (size, size))

    # Keep only the upper triangle values (including diagonal), set lower triangle to zero
    Q = np.triu(Q)


    # Ensure diagonal values are positive (bias terms)
    np.fill_diagonal(Q,np.random.uniform(node_weight_range[0], node_weight_range[1], size))


    return Q

class AdamOptimizer:
    """
    Stochastic gradient descent optimizer using the Adam optimization algorithm.

    Note: All default values are based on the original Adam paper.

    Parameters
    ----------
    params : list
        A concatenated list containing coefs_ and intercepts_ in the MLP model.
        Used for initializing velocities and updating parameters.

    learning_rate_init : float, default=0.001
        The initial learning rate used to control the step size in updating the weights.

    beta_1 : float, default=0.9
        Exponential decay rate for estimates of the first moment vector, should be in [0, 1).

    beta_2 : float, default=0.999
        Exponential decay rate for estimates of the second moment vector, should be in [0, 1).

    epsilon : float, default=1e-8
        A small value to ensure numerical stability and avoid division by zero.

    amsgrad : bool, default=False
        Whether to use the AMSGrad variant of Adam.

    Attributes
    ----------
    learning_rate : float
        The current learning rate after applying bias correction.

    t : int
        The optimization step count (timestep).

    ms : list
        First moment vectors (moving average of gradients).

    vs : list
        Second moment vectors (moving average of squared gradients).

    max_vs : list
        Maximum of past squared gradients used in AMSGrad.

    References
    ----------
    Kingma, Diederik, and Jimmy Ba.
    "Adam: A method for stochastic optimization."
    arXiv preprint arXiv:1412.6980 (2014).
    """

    def __init__(self, params, learning_rate_init=0.001, beta_1=0.9,
                 beta_2=0.999, epsilon=1e-8, amsgrad=True):

        self.beta_1 = beta_1
        self.beta_2 = beta_2
        self.epsilon = epsilon

        # Initialize learning rate as an array if provided, else a scalar
        if isinstance(learning_rate_init, float):
            self.learning_rate_init = np.ones(len(params)) * learning_rate_init
        else:
            self.learning_rate_init = np.array(learning_rate_init)

        self.t = 0  # Timestep initialization
        self.ms = [np.zeros_like(param) for param in params]  # First moment vector (m)
        self.vs = [np.zeros_like(param) for param in params]  # Second moment vector (v)
        self.amsgrad = amsgrad
        self.max_vs = [np.zeros_like(param) for param in params]  # For AMSGrad correction

    def get_updates(self, grads):
        """
        Computes the parameter updates based on the provided gradients.

        Parameters
        ----------
        grads : list
            Gradients with respect to coefs_ and intercepts_ in the model.

        Returns
        -------
        updates : list
            The values to be added to params for optimization.
        """
        self.t += 1  # Increment timestep

        # Update biased first moment estimate (m)
        self.ms = [self.beta_1 * m + (1 - self.beta_1) * grad
                   for m, grad in zip(self.ms, grads)]

        # Update biased second moment estimate (v)
        self.vs = [self.beta_2 * v + (1 - self.beta_2) * (grad ** 2)
                   for v, grad in zip(self.vs, grads)]

        # Update maximum second moment for AMSGrad if enabled
        self.max_vs = [np.maximum(v, max_v) for v, max_v in zip(self.vs, self.max_vs)]

        # Compute bias-corrected learning rate
        self.learning_rate = (self.learning_rate_init *
                              np.sqrt(1 - self.beta_2 ** self.t) /
                              (1 - self.beta_1 ** self.t))

        # Compute update step based on AMSGrad condition
        if self.amsgrad:
            updates = [lr * m / (np.sqrt(max_v) + self.epsilon)
                       for lr, m, max_v in zip(self.learning_rate, self.ms, self.max_vs)]
        else:
            updates = [lr * m / (np.sqrt(v) + self.epsilon)
                       for lr, m, v in zip(self.learning_rate, self.ms, self.vs)]

        return updates





class QAOA_layer:

    def __init__(self, depth, Q):
        """
        A class to represent a layer of a Quantum Approximate Optimization Algorithm (QAOA).

        Parameters
        ----------
        depth : int
            The number of QAOA layers (depth of the circuit).

        Q : np.ndarray
            The QUBO matrix representing the quadratic unconstrained binary optimization problem.

        Attributes
        ----------
        Q : np.ndarray
            The QUBO matrix.

        p : int
            The depth of the QAOA circuit.

        ham : qml.Hamiltonian
            The cost Hamiltonian for the given QUBO problem.

        dev : qml.device
            The quantum device used for simulation.

        """
        self.Q = Q  # Store the QUBO matrix
        self.p = depth  # Store the QAOA depth
        self.ham = self.prepare_cost_hamiltonian()  # Prepare the cost Hamiltonian based on QUBO matrix
        self.dev = qml.device("default.qubit", wires=Q.shape[0])  # Quantum device with qubits equal to Q size

    def qaoa_circuit(self, params):
        """
        Constructs the QAOA circuit based on given parameters.

        Parameters
        ----------
        params : list
            A list containing gamma and beta values for parameterized QAOA layers.
        """
        n = self.Q.shape[0]  # Number of qubits based on QUBO matrix size
        gammas = params[:self.p]  # Extract gamma parameters
        betas = params[self.p:]  # Extract beta parameters

        # Apply Hadamard gates to all qubits for uniform superposition
        for i in range(n):
            qml.Hadamard(wires=i)

        # Apply QAOA layers consisting of cost and mixer Hamiltonians
        for layer in range(self.p):
            self.qubo_cost(gammas[layer])
            self.mixer(betas[layer])

    def qubo_cost(self, gamma):
        """
        Implements the cost Hamiltonian evolution for the QUBO problem.

        Parameters
        ----------
        gamma : float
            Parameter for cost Hamiltonian evolution.
        """
        n = self.Q.shape[0]
        for i in range(n):
            for j in range(n):
                if self.Q[i, j] != 0:
                    if i == j:
                        qml.RZ(2 * gamma * float(self.Q[i, j]), wires=i)
                    else:
                        """ qml.CNOT(wires=[i, j])
                        qml.RZ(2 * gamma * float(self.Q[i, j]), wires=j)
                        qml.CNOT(wires=[i, j]) """
                        qml.MultiRZ(2 * gamma * float(self.Q[i, j]),wires=[i,j])

    def mixer(self, beta):
        """
        Implements the mixer Hamiltonian for QAOA.

        Parameters
        ----------
        beta : float
            Parameter for mixer Hamiltonian evolution.
        """
        for i in range(self.Q.shape[0]):
            qml.RX(2 * beta, wires=i)

    def prepare_cost_hamiltonian(self):
        """
        Constructs the cost Hamiltonian for the QUBO problem.

        Returns
        -------
        qml.Hamiltonian
            The constructed cost Hamiltonian.
        """
        n = self.Q.shape[0]
        coeffs = []
        ops = []

        for i in range(n):
            for j in range(n):
                if self.Q[i, j] != 0:
                    if i == j:
                        coeffs.append(self.Q[i, j])
                        ops.append(qml.PauliZ(i))
                    else:
                        coeffs.append(self.Q[i, j])
                        ops.append(qml.PauliZ(i) @ qml.PauliZ(j))

        return qml.Hamiltonian(coeffs, ops)



def add_zero_row_col(matrix, m):
    """
    Adds a new row and column filled with zeros at the specified position
    in an n x n matrix, resulting in an (n+1) x (n+1) matrix.

    Args:
        matrix (np.array): The original n x n matrix.
        m (int): The index (0-based) where the new row and column will be inserted.

    Returns:
        np.array: The expanded (n+1) x (n+1) matrix with the new row and column filled with zeros.
    """
    n = matrix.shape[0]  # Get the size of the original matrix

    # Create a new (n+1)x(n+1) matrix initialized with zeros
    new_matrix = np.zeros((n + 1, n + 1))

    # Copy the top-left submatrix (before row m and column m)
    new_matrix[:m, :m] = matrix[:m, :m]

    # Copy the top-right submatrix (after column m)
    new_matrix[:m, m+1:] = matrix[:m, m:]

    # Copy the bottom-left submatrix (after row m)
    new_matrix[m+1:, :m] = matrix[m:, :m]

    # Copy the bottom-right submatrix (after row m and column m)
    new_matrix[m+1:, m+1:] = matrix[m:, m:]

    # The new row (index m) and column (index m) remain zeros by default

    return new_matrix






def reduce_hamiltonian(J, k, l, node_assignments, sign):
    """
    Reduces the given Hamiltonian matrix by applying the constraint Z_k = sign * Z_l.

    Args:
        J (np.array): The initial Hamiltonian matrix (including diagonal terms).
        k (int): Index of the variable to be removed.
        l (int): Index of the variable to be replaced.
        node_assignments (dict): Dictionary mapping node indices to assignments.
        sign (int): Relationship (1 if identical, -1 if opposite).

    Returns:
        tuple:
            - np.array: The reduced Hamiltonian matrix with the k-th variable removed.
            - np.array: An expanded version of the reduced matrix with extra rows and columns added back.
    """
    # Update interactions: J[i, l] = J[i, l] + sign * J[i, k]
    J_res = copy.deepcopy(J)

    for i in range(J.shape[0]):
        if i != k and i != l:
            J_res[i, l] += sign * J_res[i, k]  # Update row
            J_res[l, i] += sign * J_res[k, i]  # Update column

    # Update diagonal elements (self-interaction term)
    J_res[l, l] = sign * J_res[k, k] + J_res[l, l]

    # Zero out lower triangular elements to maintain upper triangular form
    J_res = zero_lower_triangle(J_res)

    # Sort keys for correct row/column addition
    key_list = sorted(node_assignments.keys())

    # Set the removed row and column to zero before deletion
    J_res[:, k] = 0
    J_res[k, :] = 0

    # Remove the k-th row and column
    J_res = np.delete(J_res, k, axis=0)
    J_res = np.delete(J_res, k, axis=1)

    # Create a copy for expansion
    R = copy.deepcopy(J_res)

    # Add back zero rows and columns at specified indices
    for key in key_list:
        R = add_zero_row_col(R, key)
    return J_res, R

def add_zero_row_col(matrix, m):
    """
    Adds a new row and column filled with zeros at the specified position
    in an n x n matrix, resulting in an (n+1) x (n+1) matrix.

    Args:
        matrix (np.array): The original n x n matrix.
        m (int): The index (0-based) where the new row and column will be inserted.

    Returns:
        np.array: The expanded (n+1) x (n+1) matrix with the new row and column filled with zeros.
    """
    n = matrix.shape[0]

    # Create a new (n+1)x(n+1) matrix initialized with zeros
    new_matrix = np.zeros((n + 1, n + 1))

    # Copy the existing elements to the new matrix
    new_matrix[:m, :m] = matrix[:m, :m]      # Top-left block
    new_matrix[:m, m+1:] = matrix[:m, m:]    # Top-right block
    new_matrix[m+1:, :m] = matrix[m:, :m]    # Bottom-left block
    new_matrix[m+1:, m+1:] = matrix[m:, m:]  # Bottom-right block

    return new_matrix

def signed_softmax_rewards(rewards, beta=15.0):
    """
    Apply softmax transformation to absolute values of rewards
    while preserving their original sign.

    Args:
        rewards (np.ndarray): Array of reward values.
        beta (float): Temperature parameter to control sharpness of softmax.

    Returns:
        np.ndarray: Transformed reward values with preserved sign.
    """
    rewards = np.array(rewards)

    # Step 1: Compute absolute values and apply softmax
    abs_rewards = np.abs(rewards)
    scaled_rewards = beta * abs_rewards
    exp_rewards = np.exp(scaled_rewards - np.max(scaled_rewards))  # Numerical stability
    softmax_vals = exp_rewards / np.sum(exp_rewards)

    # Step 2: Restore original sign
    signed_rewards = np.sign(rewards) * softmax_vals
    return signed_rewards




class Edge:
    """
    Represents an edge in the graph connecting nodes with a "same" or "different" condition.

    Attributes:
        value (int): The target node index.
        same (bool): True if nodes must have the same value, False if they must have different values.
    """
    def __init__(self, edge_type, value):
        self.value = value
        self.same = edge_type == "same"  # True if 'same', False if 'diff'

def dfs(node, node_number, edge_list, tmp_list, result_list):
    """
    Depth-First Search (DFS) function to explore valid assignments of values to nodes
    based on the given same/different constraints.

    Args:
        node (int): Current node index being processed.
        node_number (int): Total number of nodes.
        edge_list (dict): Adjacency list representation of constraints.
        tmp_list (list): Temporary list storing current node assignments.
        result_list (list): List to store valid assignment combinations.

    Returns:
        None
    """
    if node == node_number:
        result_list.append(copy.deepcopy(tmp_list))
        return

    if len(edge_list[node]) == 0:
        # Assign possible values (-1 or 1) and continue exploring
        tmp_list[node] = 1
        dfs(node + 1, node_number, edge_list, tmp_list, result_list)
        tmp_list[node] = -1
        dfs(node + 1, node_number, edge_list, tmp_list, result_list)
        return

    tmp_list[node] = 0  # Reset node value
    for e in edge_list[node]:
        if e.same:
            res = tmp_list[e.value]
        else:
            res = -1 * tmp_list[e.value]

        # Conflict check
        if tmp_list[node] != 0 and tmp_list[node] != res:
            return

        tmp_list[node] = res

    dfs(node + 1, node_number, edge_list, tmp_list, result_list)
    return

def get_case(same_list, diff_list, node_number):
    """
    Generates all possible assignments of values to nodes that satisfy given same/different constraints.

    Args:
        same_list (list of tuples): List of node pairs that must have the same value.
        diff_list (list of tuples): List of node pairs that must have different values.
        node_number (int): Total number of nodes.

    Returns:
        list: A list of valid node value assignments satisfying all constraints.
    """
    edge_list = {i: [] for i in range(node_number)}

    # Create "same" edges (bi-directional relationships)
    for e in same_list:
        if e[0] < e[1]:
            edge_list[e[1]].append(Edge('same', e[0]))
        else:
            edge_list[e[0]].append(Edge('same', e[1]))

    # Create "different" edges (bi-directional relationships)
    for e in diff_list:
        if e[0] < e[1]:
            edge_list[e[1]].append(Edge('diff', e[0]))
        else:
            edge_list[e[0]].append(Edge('diff', e[1]))

    # Initialize temporary storage and results list
    tmp_list = [0] * node_number
    result_list = []

    # Start DFS traversal
    dfs(0, node_number, edge_list, tmp_list, result_list)

    return result_list












