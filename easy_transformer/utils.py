from __future__ import annotations
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import gc
import datasets
import einops
from transformers import AutoTokenizer
import random
from typing import Optional, Union, Tuple, List, Dict
from torchtyping import TensorType as TT
import transformers
from huggingface_hub import hf_hub_download
import re
from functools import lru_cache
from rich import print as rprint

CACHE_DIR = transformers.TRANSFORMERS_CACHE
import json


def download_file_from_hf(
    repo_name, file_name, subfolder=".", cache_dir=CACHE_DIR, force_is_torch=False
):
    """
    Helper function to download files from the HuggingFace Hub, from subfolder/file_name in repo_name, saving locally to cache_dir and returning the loaded file (if a json or Torch object) and the file path otherwise.

    If it's a Torch file without the ".pth" extension, set force_is_torch=True to load it as a Torch object.
    """
    file_path = hf_hub_download(
        repo_id=repo_name, filename=file_name, subfolder=subfolder, cache_dir=cache_dir
    )

    if file_path.endswith(".pth") or force_is_torch:
        return torch.load(file_path)
    elif file_path.endswith(".json"):
        return json.load(open(file_path, "r"))
    else:
        print("File type not supported:", file_path.split(".")[-1])
        return file_path


def get_sample_from_dataset(sequences, nb_sample=2, print_len=10):
    rd_idx = np.random.randint(0, len(sequences), 3)
    return "\n".join([str(sequences[k][:print_len]) + " ... " for k in rd_idx])


def print_gpu_mem(step_name=""):
    print(
        f"{step_name} ~ {np.round(torch.cuda.memory_allocated()/2e30, 2)} GiB allocated on GPU."
    )


def get_corner(tensor, n=3):
    # Prints the top left corner of the tensor
    return tensor[tuple(slice(n) for _ in range(tensor.ndim))]


def to_numpy(tensor, flat=False):
    if (type(tensor) != torch.Tensor) and (
        type(tensor) != torch.nn.parameter.Parameter
    ):
        return tensor
    if flat:
        return tensor.flatten().detach().cpu().numpy()
    else:
        return tensor.detach().cpu().numpy()


def lm_cross_entropy_loss(
    logits: TT["batch", "pos", "d_vocab"],
    tokens: TT["batch", "pos"],
    return_per_token: bool = False,
) -> Union[TT[()], TT["batch", "pos"]]:
    """Cross entropy loss for the language model, gives the loss for predicting the NEXT token.

    Args:
        logits (torch.Tensor): Logits. Shape [batch, pos, d_vocab]
        tokens (torch.Tensor[int64]): Input tokens. Shape [batch, pos]
        return_per_token (bool, optional): Whether to return the log probs predicted for the correct token, or the loss (ie mean of the predicted log probs). Note that the returned array has shape [batch, seq-1] as we cannot predict the first token (alternately, we ignore the final logit). Defaults to False.
    """
    log_probs = F.log_softmax(logits, dim=-1)
    # Use torch.gather to find the log probs of the correct tokens
    # Offsets needed because we're predicting the NEXT token (this means the final logit is meaningless)
    # None and [..., 0] needed because the tensor used in gather must have the same rank.
    predicted_log_probs = log_probs[..., :-1, :].gather(
        dim=-1, index=tokens[..., 1:, None]
    )[..., 0]
    if return_per_token:
        return -predicted_log_probs
    else:
        return -predicted_log_probs.mean()


def lm_accuracy(
    logits: TT["batch", "pos", "d_vocab"],
    tokens: TT["batch", "pos"],
    return_per_token: bool = False,
) -> Union[TT[()], TT["batch", "pos"]]:
    """Cross-Entropy Accuracy for Language Modelling. We measure the accuracy on the logits for predicting the NEXT token.

    If return_per_token is True, returns the boolean for top 1 accuracy for each token in the batch. Note that this has size [batch, seq_len-1], as we cannot predict the first token.
    """
    top_prediction = logits.argmax(dim=-1)
    correct_matches = top_prediction[:, :-1] == tokens[:, 1:]
    if return_per_token:
        return correct_matches
    else:
        return correct_matches.sum() / correct_matches.numel()


def gelu_new(input: TT["batch", "pos", "d_mlp"]) -> TT["batch", "pos", "d_mlp"]:
    # Implementation of GeLU used by GPT2 - subtly different from PyTorch's
    return (
        0.5
        * input
        * (
            1.0
            + torch.tanh(
                np.sqrt(2.0 / np.pi) * (input + 0.044715 * torch.pow(input, 3.0))
            )
        )
    )


def gelu_fast(input: TT["batch", "pos", "d_mlp"]) -> TT["batch", "pos", "d_mlp"]:
    return (
        0.5
        * input
        * (1.0 + torch.tanh(input * 0.7978845608 * (1.0 + 0.044715 * input * input)))
    )


def solu(input: TT["batch", "pos", "d_mlp"]) -> TT["batch", "pos", "d_mlp"]:
    """
    SoLU activation function as described by
    https://transformer-circuits.pub/2022/solu/index.html.

    LayerNorm implemented by the MLP class.
    """
    return input * F.softmax(input, dim=-1)


def keep_single_column(dataset: datasets.arrow_dataset.Dataset, col_name: str):
    """
    Acts on a HuggingFace dataset to delete all columns apart from a single column name - useful when we want to tokenize and mix together different strings
    """
    for key in dataset.features:
        if key != col_name:
            dataset = dataset.remove_columns(key)
    return dataset


def tokenize_and_concatenate(
    dataset: datasets.Dataset,
    tokenizer: AutoTokenizer,
    streaming: bool = False,
    max_length: int = 1024,
    column_name: str = "text",
    add_bos_token: bool = True,
    num_proc: int = 10,
) -> datasets.Dataset:
    """Helper function to tokenizer and concatenate a dataset of text. This converts the text to tokens, concatenates them (separated by EOS tokens) and then reshapes them into a 2D array of shape (____, sequence_length), dropping the last batch. Tokenizers are much faster if parallelised, so we chop the string into 20, feed it into the tokenizer, in parallel with padding, then remove padding at the end.

    This tokenization is useful for training language models, as it allows us to efficiently train on a large corpus of text of varying lengths (without, eg, a lot of truncation or padding). Further, for models with absolute positional encodings, this avoids privileging early tokens (eg, news articles often begin with CNN, and models may learn to use early positional encodings to predict these)

    Args:
        dataset (datasets.arrow_dataset.Dataset): The dataset to tokenize, assumed to be a HuggingFace text dataset.
        tokenizer (AutoTokenizer): The tokenizer. Assumed to have a bos_token_id and an eos_token_id.
        streaming (bool, optional): Whether the dataset is being streamed. If True, avoids using parallelism. Defaults to False.
        max_length (int, optional): The length of the context window of the sequence. Defaults to 1024.
        column_name (str, optional): The name of the text column in the dataset. Defaults to 'text'.
        add_bos_token (bool, optional): . Defaults to True.

    Returns:
        datasets.arrow_dataset.Dataset: Returns the tokenized dataset, as a dataset of tensors, with a single column called "tokens"

    Note: There is a bug when inputting very small datasets (eg, <1 batch per process) where it just outputs nothing. I'm not super sure why
    """
    dataset = keep_single_column(dataset, column_name)
    if tokenizer.pad_token is None:
        # We add a padding token, purely to implement the tokenizer. This will be removed before inputting tokens to the model, so we do not need to increment d_vocab in the model.
        tokenizer.add_special_tokens({"pad_token": "<PAD>"})
    # Define the length to chop things up into - leaving space for a bos_token if required
    if add_bos_token:
        seq_len = max_length - 1
    else:
        seq_len = max_length

    def tokenize_function(examples: Dict[str, List[str]]) -> Dict[str, np.ndarray]:
        text = examples[column_name]
        # Concatenate it all into an enormous string, separated by eos_tokens
        full_text = tokenizer.eos_token.join(text)
        # Divide into 20 chunks of ~ equal length
        num_chunks = 20
        chunk_length = (len(full_text) - 1) // num_chunks + 1
        chunks = [
            full_text[i * chunk_length : (i + 1) * chunk_length]
            for i in range(num_chunks)
        ]
        # Tokenize the chunks in parallel. Uses NumPy because HuggingFace map doesn't want tensors returned
        tokens = tokenizer(chunks, return_tensors="np", padding=True)[
            "input_ids"
        ].flatten()
        # Drop padding tokens
        tokens = tokens[tokens != tokenizer.pad_token_id]
        num_tokens = len(tokens)
        num_batches = num_tokens // (seq_len)
        # Drop the final tokens if not enough to make a full sequence
        tokens = tokens[: seq_len * num_batches]
        tokens = einops.rearrange(
            tokens, "(batch seq) -> batch seq", batch=num_batches, seq=seq_len
        )
        if add_bos_token:
            prefix = np.full((num_batches, 1), tokenizer.bos_token_id)
            tokens = np.concatenate([prefix, tokens], axis=1)
        return {"tokens": tokens}

    tokenized_dataset = dataset.map(
        tokenize_function,
        batched=True,
        num_proc=(num_proc if not streaming else None),
        remove_columns=[column_name],
    )
    tokenized_dataset.set_format(type="torch", columns=["tokens"])
    return tokenized_dataset


""" 
Test ^

data = datasets.Dataset.from_dict({"text":[str(i) for i in range(1000)]})
tokenizer = AutoTokenizer.from_pretrained("NeelNanda/gpt-neox-tokenizer-digits")
print(data)
tokenize_and_concatenate(data, tokenizer, streaming=False, column_name="text")
"""


def set_seed_everywhere(seed: int):
    torch.manual_seed(seed)
    random.seed(seed)
    np.random.seed(seed)


def sample_logits(
    final_logits: TT["batch", "d_vocab"],
    top_k: Optional[int] = None,
    top_p: Optional[int] = None,
    temperature: float = 1.0,
    freq_penalty: float = 0.0,
    tokens: Optional[TT["batch", "pos"]] = None,
) -> TT["batch"]:
    """
    Sample from the logits, in order to generate text

    final_logits has shape [batch, vocab_size]
    We divide the logits by temperature before softmaxing and sampling - high temperature = more uniform, low = more argmaxy. Temp = 0.0 is greedy sampling
    We apply top_k and top_p filtering to the logits, to encourage diversity. top_k = 10 means we only sample from the 10 most likely tokens. top_p = 0.9 means we only sample from the top 90% of tokens, and then renormalise the distribution. top_k and top_p are mutually exclusive. By default we apply neither and just sample from the full distribution.

    Frequency penalty is a penalty on the probability of a token, proportional to the number of times it has been generated so far. This encourages the model to generate new tokens, rather than repeating itself. It is a hyperparameter, and should be tuned. It is applied to the logits before sampling. If this is non-zero it is required to input the input_tokens

    #! TODO: Finish testing all the edge cases here. Useful testing code:
    logits = torch.randn(4)
    print(logits)
    np.unique(np.array([sample_logits(logits, top_k=2).item() for i in range(1000)]), return_counts=True)
    """
    if temperature == 0.0:
        # Greedy sampling
        return final_logits.argmax(dim=-1)
    else:
        # Sample from the distribution

        final_logits = final_logits / temperature
        if freq_penalty > 0:
            assert (
                tokens is not None
            ), "Must provide input_tokens if applying a frequency penalty"
            for batch_index in range(final_logits.shape[0]):
                # torch.bincount returns a tensor of length d_vocab, with the number of occurences of each token in the tokens.
                final_logits[batch_index] = final_logits[
                    batch_index
                ] - freq_penalty * torch.bincount(
                    tokens[batch_index], minlength=final_logits.shape[-1]
                )
        if top_k is not None:
            assert top_k > 0, "top_k has to be greater than 0"
            top_logits, top_idx = final_logits.topk(top_k, dim=-1)
            indices_to_remove = final_logits < top_logits[..., -1].unsqueeze(-1)
            final_logits = final_logits.masked_fill(indices_to_remove, -float("inf"))
        elif top_p is not None:
            assert 1.0 >= top_p > 0.0, "top_p has to be in [0, 1)"
            sorted_logits, sorted_indices = torch.sort(final_logits, descending=True)
            cumulative_probs = sorted_logits.softmax(dim=-1).cumsum(dim=-1)
            # We round up - we want prob >= top_p not <top_p
            sorted_indices_to_remove = cumulative_probs > top_p
            sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[
                ..., :-1
            ].clone()
            sorted_indices_to_remove[..., 0] = 0
            indices_to_remove = sorted_indices_to_remove.scatter(
                -1, sorted_indices, sorted_indices_to_remove
            )
            final_logits = final_logits.masked_fill(indices_to_remove, -float("inf"))
        return torch.distributions.categorical.Categorical(logits=final_logits).sample()


# %%
# Type alias
SliceInput = Optional[
    Union[int, Tuple[int, int], Tuple[int, int, int], List[int], torch.Tensor]
]


class Slice:
    """
    We use a custom slice syntax because Python/Torch's don't let us reduce the number of dimensions:

    Note that slicing with input_slice=None means do nothing, NOT add an extra dimension (use unsqueeze for that)

    There are several modes:
    int - just index with that integer (decreases number of dimensions)
    slice - Input is a tuple converted to a slice ((k,) means :k, (k, m) means m:k, (k, m, n) means m:k:n)
    array - Input is a list or tensor or numpy array, converted to a numpy array, and we take the stack of values at those indices
    identity - Input is None, leave it unchanged.

    Examples for dim=0:
    if input_slice=0, tensor -> tensor[0]
    elif input_slice = (1, 5), tensor -> tensor[1:5]
    elif input_slice = (1, 5, 2), tensor -> tensor[1:5:2] (ie indexing with [1, 3])
    elif input_slice = [1, 4, 5], tensor -> tensor[[1, 4, 5]] (ie changing the first axis to have length 3, and taking the indices 1, 4, 5 out).
    elif input_slice is a Tensor, same as list - Tensor is assumed to be a 1D list of indices.
    """

    def __init__(
        self,
        input_slice: SliceInput = None,
    ):
        if type(input_slice) == tuple:
            input_slice = slice(*input_slice)
            self.slice = input_slice
            self.mode = "slice"
        elif type(input_slice) == int:
            self.slice = input_slice
            self.mode = "int"
        elif type(input_slice) == slice:
            self.slice = input_slice
            self.mode = "slice"
        elif (
            type(input_slice) == list
            or type(input_slice) == torch.Tensor
            or type(input_slice) == np.ndarray
        ):
            self.slice = to_numpy(input_slice)
            self.mode = "array"
        elif input_slice is None:
            self.slice = slice(None)
            self.mode = "identity"
        else:
            raise ValueError(f"Invalid input_slice {input_slice}")

    def apply(self, tensor, dim=0):
        """
        Takes in a tensor and a slice, and applies the slice to the given dimension (supports positive and negative dimension syntax). Returns the sliced tensor.
        """
        ndim = tensor.ndim
        slices = [slice(None)] * ndim
        slices[dim] = self.slice
        return tensor[tuple(slices)]

    def indices(self, max_ctx=None):
        """
        Returns the indices when this slice is applied to an axis of size max_ctx. Returns them as a numpy array, for integer slicing it is eg array([4])
        """
        if self.mode == "int":
            return np.array([self.slice])
        else:
            return np.arange(max_ctx)[self.slice]

    def __repr__(self):
        return f"Slice: {self.slice} Mode: {self.mode} "


# %%


def act_name(
    name: str,
    layer: Optional[int] = None,
    layer_type: Optional[str] = None,
):
    """
    Helper function to convert shorthand to an activation name. Pretty hacky, intended to be useful for short feedback loop hacking stuff together, more so than writing good, readable code. But it is deterministic!

    eg:
    act_name('k', 6, 'a')=='blocks.6.attn.hook_k'
    act_name('pre', 2)=='blocks.2.mlp.hook_pre'
    act_name('embed')=='hook_embed'
    act_name('normalized', 27, 'ln2')=='blocks.27.ln2.hook_normalized'
    act_name('k6')=='blocks.6.attn.hook_k'
    act_name('scale4ln1')=='blocks.4.ln1.hook_scale'
    act_name('pre5')=='blocks.5.mlp.hook_pre'
    """
    if (
        ("." in name or name.startswith("hook_"))
        and layer is None
        and layer_type is None
    ):
        # If this was called on a full name, just return it
        return name
    match = re.match(r"([a-z]+)(\d+)([a-z]?.*)", name)
    if match is not None:
        name, layer, layer_type = match.groups(0)

    layer_type_dict = {
        "a": "attn",
        "m": "mlp",
        "b": "",
        "block": "",
        "blocks": "",
        "attention": "attn",
    }
    act_name = ""
    if layer is not None:
        act_name += f"blocks.{layer}."
    if name in ["k", "v", "q", "result", "attn", "attn_scores"]:
        layer_type = "attn"
    elif name in ["pre", "post", "mid"]:
        layer_type = "mlp"
    elif layer_type in layer_type_dict:
        layer_type = layer_type_dict[layer_type]

    if layer_type:
        act_name += f"{layer_type}."
    act_name += f"hook_{name}"
    return act_name


def remove_batch_dim(tensor: TT[1, ...]) -> TT[...]:
    """
    Removes the first dimension of a tensor if it is size 1, otherwise returns the tensor unchanged
    """
    if tensor.shape[0] == 1:
        return tensor.squeeze(0)
    else:
        return tensor


def test_prompt(
    prompt: str,
    answer: str,
    model: EasyTransformer,
    prepend_space_to_answer: bool = True,
    print_details: bool = True,
    prepend_bos: bool = True,
    top_k: int = 10,
):
    """
    Function to test whether a model can give the correct answer to a prompt. Intended for exploratory analysis, so it prints things out rather than returning things.

    Works for multi-token answers and multi-token prompts.

    Will always print the ranks of the answer tokens, and if print_details will print the logit and prob for the answer tokens and the top k tokens returned for each answer position.
    """
    if prepend_space_to_answer and not answer.startswith(" "):
        answer = " " + answer
    # GPT-2 often treats the first token weirdly, so lets give it a resting position
    tokens = model.to_tokens(prompt + answer, prepend_bos=prepend_bos)
    prompt_str_tokens = model.to_str_tokens(prompt, prepend_bos=prepend_bos)
    answer_str_tokens = model.to_str_tokens(answer, prepend_bos=False)
    prompt_length = len(prompt_str_tokens)
    answer_length = len(answer_str_tokens)
    if print_details:
        print("Tokenized prompt:", prompt_str_tokens)
        print("Tokenized answer:", answer_str_tokens)
    logits = remove_batch_dim(model(tokens))
    probs = logits.softmax(dim=-1)
    answer_ranks = []
    for index in range(prompt_length, prompt_length + answer_length):
        answer_token = tokens[0, index]
        answer_str_token = answer_str_tokens[index - prompt_length]
        # Offset by 1 because models predict the NEXT token
        token_probs = probs[index - 1]
        sorted_token_probs, sorted_token_values = token_probs.sort(descending=True)
        # Janky way to get the index of the token in the sorted list - I couldn't find a better way?
        correct_rank = torch.arange(len(sorted_token_values))[
            sorted_token_values == answer_token
        ].item()
        answer_ranks.append((answer_str_token, correct_rank))
        if print_details:
            # String formatting syntax - the first number gives the number of characters to pad to, the second number gives the number of decimal places.
            # rprint gives rich text printing
            rprint(
                f"Performance on answer token:\n[b]Rank: {correct_rank: <8} Logit: {logits[index-1, answer_token].item():5.2f} Prob: {token_probs[answer_token].item():6.2%} Token: |{answer_str_token}|[/b]"
            )
            for i in range(top_k):
                print(
                    f"Top {i}th token. Logit: {logits[index-1, sorted_token_values[i]].item():5.2f} Prob: {sorted_token_probs[i].item():6.2%} Token: |{model.to_string(sorted_token_values[i])}|"
                )
    rprint(f"[b]Ranks of the answer tokens:[/b] {answer_ranks}")


# %%
def transpose(tensor: TT[..., "a", "b"]) -> TT[..., "b", "a"]:
    """
    Utility to swap the last two dimensions of a tensor, regardless of the number of leading dimensions
    """
    return tensor.transpose(-1, -2)


class FactoredMatrix:
    """
    Class to represent low rank factored matrices, where the matrix is represented as a product of two matrices. Has utilities for efficient calculation of eigenvalues, norm and SVD.
    """

    def __init__(self, A: TT[..., "ldim", "mdim"], B: TT[..., "mdim", "rdim"]):
        self.A = A
        self.B = B
        assert self.A.size(-1) == self.B.size(
            -2
        ), f"Factored matrix must match on inner dimension, shapes were a: {self.A.shape}, b:{self.B.shape}"
        self.ldim = self.A.size(-2)
        self.rdim = self.B.size(-1)
        self.mdim = self.B.size(-2)
        self.has_leading_dims = (self.A.ndim > 2) or (self.B.ndim > 2)
        self.shape = torch.broadcast_shapes(self.A.shape[:-2], self.B.shape[:-2]) + (
            self.ldim,
            self.rdim,
        )
        self.A = self.A.broadcast_to(self.shape[:-2] + (self.ldim, self.mdim))
        self.B = self.B.broadcast_to(self.shape[:-2] + (self.mdim, self.rdim))

    def __matmul__(
        self, other: Union[TT[..., "rdim", "new_rdim"], TT["rdim"], FactoredMatrix]
    ) -> Union[FactoredMatrix, TT[..., "ldim"]]:
        if isinstance(other, torch.Tensor):
            if other.ndim < 2:
                # It's a vector, so we collapse the factorisation and just return a vector
                # Squeezing/Unsqueezing is to preserve broadcasting working nicely
                return (self.A @ (self.B @ other.unsqueeze(-1))).squeeze(-1)
            else:
                assert (
                    other.size(-2) == self.rdim
                ), f"Right matrix must match on inner dimension, shapes were self: {self.shape}, other:{other.shape}"
                if self.rdim > self.mdim:
                    return FactoredMatrix(self.A, self.B @ other)
                else:
                    return FactoredMatrix(self.AB, other)
        elif isinstance(other, FactoredMatrix):
            return (self @ other.A) @ other.B

    def __rmatmul__(
        self, other: Union[TT[..., "new_ldim", "ldim"], TT["ldim"], FactoredMatrix]
    ) -> Union[FactoredMatrix, TT[..., "rdim"]]:
        if isinstance(other, torch.Tensor):
            assert (
                other.size(-1) == self.ldim
            ), f"Left matrix must match on inner dimension, shapes were self: {self.shape}, other:{other.shape}"
            if other.ndim < 2:
                # It's a vector, so we collapse the factorisation and just return a vector
                return ((other.unsqueeze(-2) @ self.A) @ self.B).squeeze(-1)
            elif self.ldim > self.mdim:
                return FactoredMatrix(other @ self.A, self.B)
            else:
                return FactoredMatrix(other, self.AB)
        elif isinstance(other, FactoredMatrix):
            return other.A @ (other.B @ self)

    @property
    def AB(self) -> TT["leading_dims":..., "ldim", "rdim"]:
        """The product matrix - expensive to compute, and can consume a lot of GPU memory"""
        return self.A @ self.B

    @property
    def BA(self) -> TT["leading_dims":..., "rdim", "ldim"]:
        """The reverse product. Only makes sense when ldim==rdim"""
        assert (
            self.rdim == self.ldim
        ), f"Can only take ba if ldim==rdim, shapes were self: {self.shape}"
        return self.B @ self.A

    @property
    def T(self) -> FactoredMatrix:
        return FactoredMatrix(self.B.transpose(-2, -1), self.A.transpose(-2, -1))

    @lru_cache(maxsize=None)
    def svd(
        self,
    ) -> Tuple[
        TT["leading_dims":..., "ldim", "mdim"],
        TT["leading_dims":..., "mdim"],
        TT["leading_dims":..., "rdim", "mdim"],
    ]:
        """
        Efficient algorithm for finding Singular Value Decomposition, a tuple (U, S, Vh) for matrix M st S is a vector and U, Vh are orthogonal matrices, and U @ S.diag() @ Vh.T == M

        (Note that Vh is given as the transpose of the obvious thing)
        """
        Ua, Sa, Vha = torch.svd(self.A)
        Ub, Sb, Vhb = torch.svd(self.B)
        middle = Sa[..., :, None] * transpose(Vha) @ Ub * Sb[..., None, :]
        Um, Sm, Vhm = torch.svd(middle)
        U = Ua @ Um
        Vh = Vhb @ Vhm
        S = Sm
        return U, S, Vh

    @property
    def U(self) -> TT["leading_dims":..., "ldim", "mdim"]:
        return self.svd()[0]

    @property
    def S(self) -> TT["leading_dims":..., "mdim"]:
        return self.svd()[1]

    @property
    def Vh(self) -> TT["leading_dims":..., "rdim", "mdim"]:
        return self.svd()[2]

    @property
    def eigenvalues(self) -> TT["leading_dims":..., "mdim"]:
        """Eigenvalues of AB are the same as for BA (apart from trailing zeros), because if BAv=kv ABAv = A(BAv)=kAv, so Av is an eigenvector of AB with eigenvalue k."""
        return torch.linalg.eig(self.BA).eigenvalues

    def __getitem__(self, idx: Union[int, Tuple]) -> FactoredMatrix:
        """Indexing - assumed to only apply to the leading dimensions."""
        if not isinstance(idx, tuple):
            idx = (idx,)
        length = len([i for i in idx if i is not None])
        if length <= len(self.shape) - 2:
            return FactoredMatrix(self.A[idx], self.B[idx])
        elif length == len(self.shape) - 1:
            return FactoredMatrix(self.A[idx], self.B[idx[:-1]])
        elif length == len(self.shape):
            return FactoredMatrix(
                self.A[idx[:-1]], self.B[idx[:-2] + (slice(None), idx[-1])]
            )
        else:
            raise ValueError(
                f"{idx} is too long an index for a FactoredMatrix with shape {self.shape}"
            )

    def norm(self) -> TT["leading_dims":...]:
        """
        Frobenius norm is sqrt(sum of squared singular values)
        """
        return self.S.pow(2).sum(-1).sqrt()

    def __repr__(self):
        return f"FactoredMatrix: Shape({self.shape}), Hidden Dim({self.mdim})"

    def make_even(self) -> FactoredMatrix:
        """
        Returns the factored form of (U @ S.sqrt().diag(), S.sqrt().diag() @ Vh) where U, S, Vh are the SVD of the matrix. This is an equivalent factorisation, but more even - each half has half the singular values, and orthogonal rows/cols
        """
        return FactoredMatrix(
            self.U * self.S.sqrt()[..., None, :],
            self.S.sqrt()[..., :, None] * transpose(self.Vh),
        )

    def get_corner(self, k=3):
        return get_corner(self.A[..., :k, :] @ self.B[..., :, :k], k)

    @property
    def ndim(self) -> int:
        return len(self.shape)

    def collapse_l(self) -> TT["leading_dims":..., "mdim", "rdim"]:
        """
        Collapses the left side of the factorization by removing the orthogonal factor (given by self.U). Returns a (..., mdim, rdim) tensor
        """
        return self.S[..., :, None] * transpose(self.Vh)

    def collapse_r(self) -> TT["leading_dims":..., "ldim", "mdim"]:
        """
        Analogous to collapse_l, returns a (..., ldim, mdim) tensor
        """
        return self.U * self.S[..., None, :]

    def unsqueeze(self, k: int) -> FactoredMatrix:
        return FactoredMatrix(self.A.unsqueeze(k), self.B.unsqueeze(k))

    @property
    def pair(
        self,
    ) -> Tuple[
        TT["leading_dims":..., "ldim", "mdim"], TT["leading_dims":..., "mdim", "rdim"]
    ]:
        return (self.A, self.B)


def composition_scores(
    left: FactoredMatrix, right: FactoredMatrix, broadcast_dims=True
) -> Union[
    TT["leading_dims":...], TT["leading_dims_left":..., "leading_dims_right":...]
]:
    """
    See `EasyTransformer.all_composition_scores` for documentation.
    """
    if broadcast_dims:
        r_leading = right.ndim - 2
        l_leading = left.ndim - 2
        for i in range(l_leading):
            right = right.unsqueeze(i)
        for i in range(r_leading):
            left = left.unsqueeze(i + l_leading)
    assert (
        left.rdim == right.ldim
    ), f"Composition scores require left.rdim==right.ldim, shapes were left: {left.shape}, right:{right.shape}"

    right = right.collapse_r()
    left = left.collapse_l()
    r_norms = right.norm(dim=[-2, -1])
    l_norms = left.norm(dim=[-2, -1])
    comp_norms = (left @ right).norm(dim=[-2, -1])
    return comp_norms / r_norms / l_norms


# %%
